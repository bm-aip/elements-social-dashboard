import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from supabase import create_client
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import pytz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

IST = pytz.timezone('Asia/Kolkata')

# --- Supabase ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# --- Meta ---
# Graph API version is now an env var. Meta retires a version ~2 years after
# release and silently runs expired calls on a newer version, which is how the
# old v19.0 metric names started returning empty. Keep this at the latest
# version your app supports (v22.0+ post-dates the Apr-2025 Instagram changes).
GRAPH_VER = os.environ.get("META_GRAPH_VERSION", "v22.0")
META_PAGE_ID = os.environ.get("META_PAGE_ID", "100728566307909")
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")

# Page Insights metric names are a moving target (Meta removed 89 metrics in
# Dec-2023 and further waves in Nov-2025 / Aug-2026). Each is overridable via
# env and fetched INDEPENDENTLY, so one deprecated metric degrades to
# "unavailable" instead of zeroing the whole response. Set to "" to disable.
FB_REACH_METRIC = os.environ.get("FB_REACH_METRIC", "page_impressions_unique")
FB_IMPRESSIONS_METRIC = os.environ.get("FB_IMPRESSIONS_METRIC", "page_impressions")
FB_ENGAGEMENT_METRIC = os.environ.get("FB_ENGAGEMENT_METRIC", "page_post_engagements")
# Instagram: `impressions` was removed Apr-2025 (replaced by `views`); the
# days_28 aggregate was dropped with no replacement. Fetched per-metric too.
IG_REACH_METRIC = os.environ.get("IG_REACH_METRIC", "reach")
IG_VIEWS_METRIC = os.environ.get("IG_VIEWS_METRIC", "views")

# --- YouTube ---
YT_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID")
YT_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN")
YT_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")  # auto-fetched if empty

# --- Ops ---
# Protects /api/refresh. If unset, the endpoint refuses to run (fail closed).
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN")
# Set to "0" on all-but-one gunicorn worker (or when a separate scheduler
# process is used) to avoid duplicate daily runs with WEB_CONCURRENCY > 1.
ENABLE_SCHEDULER = os.environ.get("ENABLE_SCHEDULER", "1") == "1"
RUN_FETCH_ON_STARTUP = os.environ.get("RUN_FETCH_ON_STARTUP", "0") == "1"

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"       # platform fetched, but some metrics unavailable
STATUS_FAILED = "failed"         # hard error (auth/exception) — no fresh data
STATUS_NOT_CONNECTED = "not_connected"  # account not linked / not configured


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _now():
    return datetime.now(IST).isoformat()


def fetch_single_insight(base, metric, period="days_28"):
    """Fetch ONE Page/IG insight metric in isolation.

    Returns (value_or_None, error_or_None). Requesting metrics one at a time
    means a single deprecated/invalid metric yields None for that metric only,
    instead of an 'invalid metric' error that empties the whole batch.
    """
    if not metric:
        return None, "disabled"
    try:
        r = requests.get(f"{base}/insights", params={
            "metric": metric,
            "period": period,
            "access_token": META_ACCESS_TOKEN,
        }, timeout=15)
        data = r.json()
        if "error" in data:
            msg = data["error"].get("message", str(data["error"]))
            logger.warning(f"Insight '{metric}' error: {msg}")
            return None, msg
        for item in data.get("data", []):
            values = item.get("values", [])
            if values:
                # days_28 returns a single window value; take the latest.
                return values[-1].get("value", 0), None
        return None, "no_data"
    except Exception as e:
        logger.warning(f"Insight '{metric}' exception: {e}")
        return None, str(e)


def get_last_snapshot(platform, key):
    """Most recent stored value for a platform/metric, or None."""
    if not supabase:
        return None
    try:
        res = (supabase.table("elements_social_snapshots")
               .select("metric_value")
               .eq("platform", platform).eq("metric_key", key)
               .order("captured_at", desc=True).limit(1).execute())
        if res.data:
            return res.data[0]["metric_value"]
    except Exception as e:
        logger.warning(f"last snapshot read error ({platform}/{key}): {e}")
    return None


# ─────────────────────────────────────────────
# META FETCHERS
# ─────────────────────────────────────────────

def get_instagram_account_id():
    """Instagram Business Account ID linked to the Page, or (None, reason)."""
    url = f"https://graph.facebook.com/{GRAPH_VER}/{META_PAGE_ID}"
    params = {"fields": "instagram_business_account", "access_token": META_ACCESS_TOKEN}
    r = requests.get(url, params=params, timeout=15)
    data = r.json()
    if "error" in data:
        msg = data["error"].get("message", str(data["error"]))
        logger.error(f"Graph API error fetching IG account: {msg}")
        return None, f"graph_error: {msg}"
    if "instagram_business_account" in data:
        return data["instagram_business_account"]["id"], None
    # Field is silently omitted when no IG professional account is linked to
    # this Page, OR the token lacks instagram_basic. Not an error response.
    logger.warning(f"No instagram_business_account on Page {META_PAGE_ID}: {data}")
    return None, ("Instagram Business account not linked to this Page "
                  "(or token missing instagram_basic/instagram_manage_insights)")


def fetch_facebook_metrics():
    try:
        base = f"https://graph.facebook.com/{GRAPH_VER}/{META_PAGE_ID}"

        info_r = requests.get(base, params={
            "fields": "followers_count,fan_count,name",
            "access_token": META_ACCESS_TOKEN,
        }, timeout=15).json()
        if "error" in info_r:
            return {"status": STATUS_FAILED,
                    "error": info_r["error"].get("message", str(info_r["error"]))}
        followers = info_r.get("followers_count") or info_r.get("fan_count") or 0

        reach, reach_err = fetch_single_insight(base, FB_REACH_METRIC)
        impressions, impr_err = fetch_single_insight(base, FB_IMPRESSIONS_METRIC)
        engagements, eng_err = fetch_single_insight(base, FB_ENGAGEMENT_METRIC)

        # Recent posts
        posts = []
        posts_r = requests.get(f"{base}/posts", params={
            "fields": ("id,message,story,created_time,full_picture,permalink_url,"
                       "likes.summary(true),comments.summary(true),shares"),
            "limit": 20,
            "access_token": META_ACCESS_TOKEN,
        }, timeout=15).json()
        for post in posts_r.get("data", []):
            likes = post.get("likes", {}).get("summary", {}).get("total_count", 0)
            comments = post.get("comments", {}).get("summary", {}).get("total_count", 0)
            shares = post.get("shares", {}).get("count", 0) if "shares" in post else 0
            total_eng = likes + comments + shares
            eng_rate = round((total_eng / max(followers, 1)) * 100, 2)
            posts.append({
                "platform": "facebook", "post_id": post["id"],
                "title": (post.get("message") or post.get("story") or "")[:120],
                "thumbnail": post.get("full_picture", ""),
                "likes": likes, "comments": comments, "shares": shares,
                "views": 0, "reach": 0, "engagement_rate": eng_rate,
                "published_at": post.get("created_time"),
                "post_url": post.get("permalink_url", ""),
            })

        # Fallback engagement if the page-level metric is dead: sum post engagement.
        if engagements is None and posts:
            engagements = sum(p["likes"] + p["comments"] + p["shares"] for p in posts)
            eng_err = (eng_err or "metric_unavailable") + "; using post-sum fallback"

        unavailable = [n for n, e in (("reach", reach_err), ("impressions", impr_err),
                                      ("engagement", eng_err)) if e and "fallback" not in (e or "")]
        return {
            "status": STATUS_PARTIAL if unavailable else STATUS_OK,
            "error": ("metrics unavailable: " + ", ".join(unavailable)) if unavailable else None,
            "followers": followers,
            "reach_28d": reach, "impressions_28d": impressions,
            "engagement_total": engagements, "posts": posts,
        }
    except Exception as e:
        logger.error(f"Facebook fetch error: {e}")
        return {"status": STATUS_FAILED, "error": str(e)}


def fetch_instagram_metrics():
    try:
        ig_id, reason = get_instagram_account_id()
        if not ig_id:
            return {"status": STATUS_NOT_CONNECTED, "error": reason}

        base = f"https://graph.facebook.com/{GRAPH_VER}/{ig_id}"
        info_r = requests.get(base, params={
            "fields": "followers_count,media_count,username",
            "access_token": META_ACCESS_TOKEN,
        }, timeout=15).json()
        if "error" in info_r:
            return {"status": STATUS_FAILED,
                    "error": info_r["error"].get("message", str(info_r["error"]))}
        followers = info_r.get("followers_count", 0)

        reach, reach_err = fetch_single_insight(base, IG_REACH_METRIC)
        views, views_err = fetch_single_insight(base, IG_VIEWS_METRIC)

        posts = []
        media_r = requests.get(f"{base}/media", params={
            "fields": ("id,caption,media_type,thumbnail_url,media_url,timestamp,"
                       "permalink,like_count,comments_count"),
            "limit": 20,
            "access_token": META_ACCESS_TOKEN,
        }, timeout=15).json()
        for post in media_r.get("data", []):
            likes = post.get("like_count", 0)
            comments = post.get("comments_count", 0)
            total_eng = likes + comments
            eng_rate = round((total_eng / max(followers, 1)) * 100, 2)
            thumbnail = post.get("thumbnail_url") or post.get("media_url", "")
            posts.append({
                "platform": "instagram", "post_id": post["id"],
                "title": (post.get("caption") or "")[:120], "thumbnail": thumbnail,
                "likes": likes, "comments": comments, "shares": 0,
                "views": 0, "reach": 0, "engagement_rate": eng_rate,
                "published_at": post.get("timestamp"),
                "post_url": post.get("permalink", ""),
            })

        # Account engagement = sum of recent-post engagement (page-level eng
        # metric doesn't exist for IG). avg rate = mean per-post rate.
        engagement_total = sum(p["likes"] + p["comments"] for p in posts)
        avg_eng_rate = round(sum(p["engagement_rate"] for p in posts) / len(posts), 2) if posts else 0

        unavailable = [n for n, e in (("reach", reach_err), ("views", views_err)) if e]
        return {
            "status": STATUS_PARTIAL if unavailable else STATUS_OK,
            "error": ("metrics unavailable: " + ", ".join(unavailable)) if unavailable else None,
            "followers": followers,
            "reach_28d": reach,
            "impressions_28d": views,  # Meta now reports "views" in place of impressions
            "engagement_total": engagement_total,
            "avg_engagement_rate": avg_eng_rate, "posts": posts,
        }
    except Exception as e:
        logger.error(f"Instagram fetch error: {e}")
        return {"status": STATUS_FAILED, "error": str(e)}


# ─────────────────────────────────────────────
# YOUTUBE FETCHERS
# ─────────────────────────────────────────────

def _yt_creds():
    return Credentials(
        token=None, refresh_token=YT_REFRESH_TOKEN,
        client_id=YT_CLIENT_ID, client_secret=YT_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
    )


def fetch_youtube_metrics():
    try:
        if not (YT_REFRESH_TOKEN and YT_CLIENT_ID and YT_CLIENT_SECRET):
            return {"status": STATUS_NOT_CONNECTED, "error": "YouTube OAuth env vars not set"}

        yt = build("youtube", "v3", credentials=_yt_creds())

        channel_r = yt.channels().list(part="snippet,statistics", mine=True).execute()
        if not channel_r.get("items"):
            return {"status": STATUS_FAILED, "error": "No channel for these credentials"}

        channel = channel_r["items"][0]
        stats = channel["statistics"]
        channel_id = channel["id"]
        subscribers = int(stats.get("subscriberCount", 0))
        total_views = int(stats.get("viewCount", 0))
        video_count = int(stats.get("videoCount", 0))

        # Analytics (last 28 days) — non-fatal if it fails.
        watch_time, period_views, avg_view_duration = None, None, None
        analytics_err = None
        try:
            yta = build("youtubeAnalytics", "v2", credentials=_yt_creds())
            end_date = datetime.now(IST).strftime("%Y-%m-%d")
            start_date = (datetime.now(IST) - timedelta(days=28)).strftime("%Y-%m-%d")
            analytics_r = yta.reports().query(
                ids=f"channel=={channel_id}", startDate=start_date, endDate=end_date,
                metrics="views,estimatedMinutesWatched,averageViewDuration",
                dimensions="day",
            ).execute()
            rows = analytics_r.get("rows", [])
            period_views = sum(int(r[1]) for r in rows)
            watch_time = sum(int(r[2]) for r in rows)
            if rows:
                avg_view_duration = int(sum(r[3] for r in rows) / len(rows))
        except Exception as e:
            analytics_err = str(e)
            logger.error(f"YouTube Analytics query error: {e}")

        # Top videos by view count (lifetime). NOTE: this is all-time top, not
        # recent — see follow-ups in PR notes if "recent uploads" is wanted.
        posts = []
        search_r = yt.search().list(
            part="snippet", channelId=channel_id, order="viewCount",
            type="video", maxResults=20,
        ).execute()
        video_ids = [i["id"]["videoId"] for i in search_r.get("items", [])]
        if video_ids:
            videos_r = yt.videos().list(
                part="snippet,statistics", id=",".join(video_ids)).execute()
            for video in videos_r.get("items", []):
                vstats = video["statistics"]
                snippet = video["snippet"]
                vid_views = int(vstats.get("viewCount", 0))
                likes = int(vstats.get("likeCount", 0))
                comments = int(vstats.get("commentCount", 0))
                eng_rate = round(((likes + comments) / max(vid_views, 1)) * 100, 2)
                posts.append({
                    "platform": "youtube", "post_id": video["id"],
                    "title": snippet.get("title", "")[:120],
                    "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                    "likes": likes, "comments": comments, "shares": 0,
                    "views": vid_views, "reach": 0, "engagement_rate": eng_rate,
                    "published_at": snippet.get("publishedAt"),
                    "post_url": f"https://www.youtube.com/watch?v={video['id']}",
                })

        return {
            "status": STATUS_PARTIAL if analytics_err else STATUS_OK,
            "error": f"analytics unavailable: {analytics_err}" if analytics_err else None,
            "followers": subscribers, "total_views": total_views,
            "video_count": video_count,
            "reach_28d": period_views, "impressions_28d": period_views,
            "engagement_total": sum(p["likes"] + p["comments"] for p in posts),
            "watch_time_minutes": watch_time, "avg_view_duration": avg_view_duration,
            "posts": posts,
        }
    except Exception as e:
        # invalid_grant = the OAuth refresh token is revoked/expired.
        msg = str(e)
        if "invalid_grant" in msg:
            msg = ("YouTube OAuth refresh token invalid (invalid_grant) — regenerate it; "
                   "set the OAuth consent screen to 'In production' so tokens stop expiring")
        logger.error(f"YouTube fetch error: {msg}")
        return {"status": STATUS_FAILED, "error": msg}


# ─────────────────────────────────────────────
# SUPABASE WRITE
# ─────────────────────────────────────────────

def write_summary(platform, data, follower_key):
    """Write status + whatever metrics succeeded. Metrics that are None are
    OMITTED from the payload so a failed/partial run never overwrites the last
    good value with 0 (PostgREST upsert only updates the columns you send)."""
    if not supabase:
        return
    status = data.get("status", STATUS_FAILED)
    payload = {
        "platform": platform,
        "fetch_status": status,
        "fetch_error": data.get("error"),
        "last_updated": _now(),
    }
    if status in (STATUS_OK, STATUS_PARTIAL):
        payload["last_success_at"] = _now()
        # followers / delta
        followers = data.get("followers")
        if followers is not None:
            last = get_last_snapshot(platform, follower_key)
            payload["followers"] = followers
            payload["followers_delta"] = int(followers - last) if last is not None else 0
        for col, key in (("reach_28d", "reach_28d"),
                         ("impressions_28d", "impressions_28d"),
                         ("engagement_total", "engagement_total"),
                         ("avg_engagement_rate", "avg_engagement_rate"),
                         ("video_views", "total_views"),
                         ("video_count", "video_count"),
                         ("watch_time_minutes", "watch_time_minutes")):
            val = data.get(key)
            if val is not None:
                payload[col] = val
    try:
        supabase.table("elements_social_summary").upsert(
            payload, on_conflict="platform").execute()
    except Exception as e:
        logger.error(f"Supabase summary upsert error ({platform}): {e}")


def upsert_posts(posts):
    if not supabase or not posts:
        return
    try:
        rows = [{**p, "captured_at": _now()} for p in posts]
        supabase.table("elements_social_posts").upsert(
            rows, on_conflict="post_id").execute()  # single batched call
    except Exception as e:
        logger.error(f"Supabase posts upsert error: {e}")


def store_snapshot(platform, key, value):
    """One snapshot per platform/metric/day: update today's row if present
    (e.g. a manual refresh after the cron), else insert a new one."""
    if not supabase or value is None:
        return
    try:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        existing = (supabase.table("elements_social_snapshots")
                    .select("id")
                    .eq("platform", platform).eq("metric_key", key)
                    .gte("captured_at", f"{today}T00:00:00+05:30")
                    .limit(1).execute())
        if existing.data:
            (supabase.table("elements_social_snapshots")
             .update({"metric_value": value, "captured_at": _now()})
             .eq("id", existing.data[0]["id"]).execute())
        else:
            supabase.table("elements_social_snapshots").insert({
                "platform": platform, "metric_key": key,
                "metric_value": value, "captured_at": _now(),
            }).execute()
    except Exception as e:
        logger.error(f"Supabase snapshot error: {e}")


# ─────────────────────────────────────────────
# MAIN FETCH JOB
# ─────────────────────────────────────────────

_refresh_lock = threading.Lock()
_refresh_state = {"running": False, "started_at": None, "finished_at": None, "result": None}


def run_fetch_job():
    logger.info("Starting daily fetch job...")
    result = {}

    for platform, fetcher, follower_key in (
        ("facebook", fetch_facebook_metrics, "followers"),
        ("instagram", fetch_instagram_metrics, "followers"),
        ("youtube", fetch_youtube_metrics, "subscribers"),
    ):
        data = fetcher()
        status = data.get("status", STATUS_FAILED)
        result[platform] = {"status": status, "error": data.get("error")}

        # Always record status (even on failure, so staleness is visible).
        write_summary(platform, data, follower_key)

        if status in (STATUS_OK, STATUS_PARTIAL):
            upsert_posts(data.get("posts", []))
            store_snapshot(platform, follower_key, data.get("followers"))
            store_snapshot(platform, "reach_28d", data.get("reach_28d"))
            store_snapshot(platform, "impressions_28d", data.get("impressions_28d"))
            store_snapshot(platform, "engagement_total", data.get("engagement_total"))

        logger.info(f"[{platform}] status={status} error={data.get('error')}")

    logger.info(f"Fetch job complete: {json.dumps(result)}")
    return result


def _run_fetch_async():
    with _refresh_lock:
        if _refresh_state["running"]:
            return
        _refresh_state.update(running=True, started_at=_now(), finished_at=None, result=None)

    def worker():
        try:
            res = run_fetch_job()
        except Exception as e:
            logger.error(f"Async fetch job error: {e}")
            res = {"error": str(e)}
        finally:
            with _refresh_lock:
                _refresh_state.update(running=False, finished_at=_now(), result=res)

    threading.Thread(target=worker, daemon=True).start()


# ─────────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────────

@app.route("/api/summary")
def api_summary():
    if not supabase:
        return jsonify({"error": "DB not configured"}), 500
    try:
        result = supabase.table("elements_social_summary").select("*").execute()
        return jsonify({row["platform"]: row for row in result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/posts/<platform>")
def api_posts(platform):
    if not supabase:
        return jsonify({"error": "DB not configured"}), 500
    try:
        if platform == "all":
            # 20 per platform (not 20 total), then merge newest-first.
            merged = []
            for p in ("youtube", "facebook", "instagram"):
                r = (supabase.table("elements_social_posts").select("*")
                     .eq("platform", p).order("published_at", desc=True)
                     .limit(20).execute())
                merged.extend(r.data)
            merged.sort(key=lambda x: x.get("published_at") or "", reverse=True)
            return jsonify(merged)
        r = (supabase.table("elements_social_posts").select("*")
             .eq("platform", platform).order("published_at", desc=True)
             .limit(20).execute())
        return jsonify(r.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/snapshots/<platform>/<metric>")
def api_snapshots(platform, metric):
    if not supabase:
        return jsonify({"error": "DB not configured"}), 500
    try:
        result = (supabase.table("elements_social_snapshots").select("*")
                  .eq("platform", platform).eq("metric_key", metric)
                  .order("captured_at", desc=False).limit(90).execute())
        return jsonify(result.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    # Fail closed: without a configured token the endpoint will not run, so a
    # public dashboard can't be used to drain YouTube/Meta quota.
    if not REFRESH_TOKEN:
        return jsonify({"error": "refresh disabled: REFRESH_TOKEN not set"}), 503
    supplied = request.headers.get("X-Refresh-Token") or (request.get_json(silent=True) or {}).get("token")
    if supplied != REFRESH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    if _refresh_state["running"]:
        return jsonify({"status": "already_running", "started_at": _refresh_state["started_at"]}), 409
    _run_fetch_async()  # returns immediately; avoids gunicorn worker timeout
    return jsonify({"status": "started", "started_at": _refresh_state["started_at"]}), 202


@app.route("/api/refresh/status")
def api_refresh_status():
    return jsonify(_refresh_state)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "db": bool(supabase), "time": _now()})


@app.route("/")
def index():
    return render_template("dashboard.html")


# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────

if ENABLE_SCHEDULER:
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(
        run_fetch_job, "cron", hour=6, minute=0,
        coalesce=True, max_instances=1, misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info("Scheduler started (daily 06:00 IST).")
    if RUN_FETCH_ON_STARTUP:
        _run_fetch_async()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
