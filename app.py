import os
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify
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
META_PAGE_ID = os.environ.get("META_PAGE_ID", "100728566307909")
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")

# --- YouTube ---
YT_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID")
YT_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN")
YT_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")  # auto-fetched if empty


def init_db():
    """Create tables if they don't exist."""
    if not supabase:
        logger.warning("Supabase not configured, skipping DB init")
        return
    try:
        supabase.rpc('elements_social_init', {}).execute()
    except Exception:
        pass

    # Create tables via raw SQL through Supabase
    tables_sql = """
    CREATE TABLE IF NOT EXISTS elements_social_snapshots (
        id SERIAL PRIMARY KEY,
        platform TEXT NOT NULL,
        metric_key TEXT NOT NULL,
        metric_value NUMERIC,
        captured_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS elements_social_posts (
        id SERIAL PRIMARY KEY,
        platform TEXT NOT NULL,
        post_id TEXT NOT NULL UNIQUE,
        title TEXT,
        thumbnail TEXT,
        likes INTEGER DEFAULT 0,
        comments INTEGER DEFAULT 0,
        shares INTEGER DEFAULT 0,
        views INTEGER DEFAULT 0,
        reach INTEGER DEFAULT 0,
        engagement_rate NUMERIC DEFAULT 0,
        published_at TIMESTAMPTZ,
        post_url TEXT,
        captured_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS elements_social_summary (
        id SERIAL PRIMARY KEY,
        platform TEXT NOT NULL UNIQUE,
        followers INTEGER DEFAULT 0,
        followers_delta INTEGER DEFAULT 0,
        reach_28d INTEGER DEFAULT 0,
        impressions_28d INTEGER DEFAULT 0,
        engagement_total INTEGER DEFAULT 0,
        avg_engagement_rate NUMERIC DEFAULT 0,
        video_views INTEGER DEFAULT 0,
        watch_time_minutes INTEGER DEFAULT 0,
        last_updated TIMESTAMPTZ DEFAULT NOW()
    );
    """
    logger.info("DB init attempted")


# ─────────────────────────────────────────────
# META FETCHERS
# ─────────────────────────────────────────────

def get_instagram_account_id():
    """Fetch Instagram Business Account ID linked to the Page."""
    url = f"https://graph.facebook.com/v19.0/{META_PAGE_ID}"
    params = {
        "fields": "instagram_business_account",
        "access_token": META_ACCESS_TOKEN
    }
    r = requests.get(url, params=params, timeout=15)
    data = r.json()
    if "instagram_business_account" in data:
        return data["instagram_business_account"]["id"]
    return None


def fetch_facebook_metrics():
    """Fetch Facebook page metrics."""
    try:
        base = f"https://graph.facebook.com/v19.0/{META_PAGE_ID}"

        # Page info + followers
        info_r = requests.get(base, params={
            "fields": "followers_count,fan_count,name",
            "access_token": META_ACCESS_TOKEN
        }, timeout=15).json()

        followers = info_r.get("followers_count", 0)

        # Page insights - reach, impressions, engagement
        insights_r = requests.get(f"{base}/insights", params={
            "metric": "page_impressions,page_reach,page_engaged_users,page_post_engagements",
            "period": "days_28",
            "access_token": META_ACCESS_TOKEN
        }, timeout=15).json()

        metrics = {}
        for item in insights_r.get("data", []):
            name = item["name"]
            values = item.get("values", [])
            if values:
                metrics[name] = values[-1].get("value", 0)

        reach = metrics.get("page_reach", 0)
        impressions = metrics.get("page_impressions", 0)
        engaged_users = metrics.get("page_engaged_users", 0)
        engagements = metrics.get("page_post_engagements", 0)

        # Recent posts
        posts_r = requests.get(f"{base}/posts", params={
            "fields": "id,message,story,created_time,full_picture,permalink_url,likes.summary(true),comments.summary(true),shares",
            "limit": 20,
            "access_token": META_ACCESS_TOKEN
        }, timeout=15).json()

        posts = []
        for post in posts_r.get("data", []):
            likes = post.get("likes", {}).get("summary", {}).get("total_count", 0)
            comments = post.get("comments", {}).get("summary", {}).get("total_count", 0)
            shares = post.get("shares", {}).get("count", 0) if "shares" in post else 0
            total_eng = likes + comments + shares
            eng_rate = round((total_eng / max(followers, 1)) * 100, 2)

            posts.append({
                "platform": "facebook",
                "post_id": post["id"],
                "title": (post.get("message") or post.get("story") or "")[:120],
                "thumbnail": post.get("full_picture", ""),
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "views": 0,
                "reach": 0,
                "engagement_rate": eng_rate,
                "published_at": post.get("created_time"),
                "post_url": post.get("permalink_url", "")
            })

        return {
            "followers": followers,
            "reach_28d": reach,
            "impressions_28d": impressions,
            "engagement_total": engagements,
            "engaged_users": engaged_users,
            "posts": posts
        }
    except Exception as e:
        logger.error(f"Facebook fetch error: {e}")
        return {}


def fetch_instagram_metrics():
    """Fetch Instagram business account metrics."""
    try:
        ig_id = get_instagram_account_id()
        if not ig_id:
            logger.warning("No Instagram Business Account found")
            return {}

        base = f"https://graph.facebook.com/v19.0/{ig_id}"

        # Account info
        info_r = requests.get(base, params={
            "fields": "followers_count,media_count,username",
            "access_token": META_ACCESS_TOKEN
        }, timeout=15).json()

        followers = info_r.get("followers_count", 0)

        # Insights
        insights_r = requests.get(f"{base}/insights", params={
            "metric": "reach,impressions,profile_views",
            "period": "days_28",
            "access_token": META_ACCESS_TOKEN
        }, timeout=15).json()

        metrics = {}
        for item in insights_r.get("data", []):
            values = item.get("values", [])
            if values:
                metrics[item["name"]] = sum(v.get("value", 0) for v in values)

        reach = metrics.get("reach", 0)
        impressions = metrics.get("impressions", 0)

        # Recent media
        media_r = requests.get(f"{base}/media", params={
            "fields": "id,caption,media_type,thumbnail_url,media_url,timestamp,permalink,like_count,comments_count",
            "limit": 20,
            "access_token": META_ACCESS_TOKEN
        }, timeout=15).json()

        posts = []
        for post in media_r.get("data", []):
            likes = post.get("like_count", 0)
            comments = post.get("comments_count", 0)
            total_eng = likes + comments
            eng_rate = round((total_eng / max(followers, 1)) * 100, 2)
            thumbnail = post.get("thumbnail_url") or post.get("media_url", "")

            posts.append({
                "platform": "instagram",
                "post_id": post["id"],
                "title": (post.get("caption") or "")[:120],
                "thumbnail": thumbnail,
                "likes": likes,
                "comments": comments,
                "shares": 0,
                "views": 0,
                "reach": 0,
                "engagement_rate": eng_rate,
                "published_at": post.get("timestamp"),
                "post_url": post.get("permalink", "")
            })

        total_eng = sum(p["likes"] + p["comments"] for p in posts)
        avg_eng_rate = round((total_eng / max(len(posts), 1) / max(followers, 1)) * 100, 2) if posts else 0

        return {
            "followers": followers,
            "reach_28d": reach,
            "impressions_28d": impressions,
            "engagement_total": total_eng,
            "avg_engagement_rate": avg_eng_rate,
            "posts": posts
        }
    except Exception as e:
        logger.error(f"Instagram fetch error: {e}")
        return {}


# ─────────────────────────────────────────────
# YOUTUBE FETCHERS
# ─────────────────────────────────────────────

def get_youtube_service():
    """Build authenticated YouTube service using refresh token."""
    try:
        creds = Credentials(
            token=None,
            refresh_token=YT_REFRESH_TOKEN,
            client_id=YT_CLIENT_ID,
            client_secret=YT_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token"
        )
        return build("youtube", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"YouTube auth error: {e}")
        return None


def get_youtube_analytics_service():
    """Build YouTube Analytics service."""
    try:
        creds = Credentials(
            token=None,
            refresh_token=YT_REFRESH_TOKEN,
            client_id=YT_CLIENT_ID,
            client_secret=YT_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token"
        )
        return build("youtubeAnalytics", "v2", credentials=creds)
    except Exception as e:
        logger.error(f"YouTube Analytics auth error: {e}")
        return None


def fetch_youtube_metrics():
    """Fetch YouTube channel and video metrics."""
    try:
        yt = get_youtube_service()
        if not yt:
            return {}

        # Channel stats
        channel_r = yt.channels().list(
            part="snippet,statistics",
            mine=True
        ).execute()

        if not channel_r.get("items"):
            return {}

        channel = channel_r["items"][0]
        stats = channel["statistics"]
        channel_id = channel["id"]

        subscribers = int(stats.get("subscriberCount", 0))
        total_views = int(stats.get("viewCount", 0))
        video_count = int(stats.get("videoCount", 0))

        # Analytics (last 28 days)
        yta = get_youtube_analytics_service()
        end_date = datetime.now(IST).strftime("%Y-%m-%d")
        start_date = (datetime.now(IST) - timedelta(days=28)).strftime("%Y-%m-%d")

        watch_time = 0
        period_views = 0
        avg_view_duration = 0

        if yta:
            try:
                analytics_r = yta.reports().query(
                    ids=f"channel=={channel_id}",
                    startDate=start_date,
                    endDate=end_date,
                    metrics="views,estimatedMinutesWatched,averageViewDuration",
                    dimensions="day"
                ).execute()

                rows = analytics_r.get("rows", [])
                period_views = sum(int(r[1]) for r in rows)
                watch_time = sum(int(r[2]) for r in rows)
                if rows:
                    avg_view_duration = int(sum(r[3] for r in rows) / len(rows))
            except Exception as e:
                logger.error(f"YouTube Analytics query error: {e}")

        # Top 20 videos by view count
        search_r = yt.search().list(
            part="snippet",
            channelId=channel_id,
            order="viewCount",
            type="video",
            maxResults=20
        ).execute()

        video_ids = [item["id"]["videoId"] for item in search_r.get("items", [])]

        posts = []
        if video_ids:
            videos_r = yt.videos().list(
                part="snippet,statistics",
                id=",".join(video_ids)
            ).execute()

            for video in videos_r.get("items", []):
                vstats = video["statistics"]
                snippet = video["snippet"]
                vid_views = int(vstats.get("viewCount", 0))
                likes = int(vstats.get("likeCount", 0))
                comments = int(vstats.get("commentCount", 0))
                total_eng = likes + comments
                eng_rate = round((total_eng / max(vid_views, 1)) * 100, 2)

                posts.append({
                    "platform": "youtube",
                    "post_id": video["id"],
                    "title": snippet.get("title", "")[:120],
                    "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                    "likes": likes,
                    "comments": comments,
                    "shares": 0,
                    "views": vid_views,
                    "reach": 0,
                    "engagement_rate": eng_rate,
                    "published_at": snippet.get("publishedAt"),
                    "post_url": f"https://www.youtube.com/watch?v={video['id']}"
                })

        return {
            "followers": subscribers,
            "total_views": total_views,
            "video_count": video_count,
            "reach_28d": period_views,
            "impressions_28d": period_views,
            "engagement_total": sum(p["likes"] + p["comments"] for p in posts),
            "watch_time_minutes": watch_time,
            "avg_view_duration": avg_view_duration,
            "posts": posts
        }
    except Exception as e:
        logger.error(f"YouTube fetch error: {e}")
        return {}


# ─────────────────────────────────────────────
# SUPABASE WRITE
# ─────────────────────────────────────────────

def upsert_summary(platform, data):
    if not supabase or not data:
        return
    try:
        supabase.table("elements_social_summary").upsert({
            "platform": platform,
            "followers": data.get("followers", 0),
            "reach_28d": data.get("reach_28d", 0),
            "impressions_28d": data.get("impressions_28d", 0),
            "engagement_total": data.get("engagement_total", 0),
            "avg_engagement_rate": data.get("avg_engagement_rate", 0),
            "video_views": data.get("total_views", 0),
            "watch_time_minutes": data.get("watch_time_minutes", 0),
            "last_updated": datetime.now(IST).isoformat()
        }, on_conflict="platform").execute()
    except Exception as e:
        logger.error(f"Supabase summary upsert error ({platform}): {e}")


def upsert_posts(posts):
    if not supabase or not posts:
        return
    try:
        for post in posts:
            supabase.table("elements_social_posts").upsert(
                {**post, "captured_at": datetime.now(IST).isoformat()},
                on_conflict="post_id"
            ).execute()
    except Exception as e:
        logger.error(f"Supabase posts upsert error: {e}")


def store_snapshot(platform, key, value):
    if not supabase:
        return
    try:
        supabase.table("elements_social_snapshots").insert({
            "platform": platform,
            "metric_key": key,
            "metric_value": value,
            "captured_at": datetime.now(IST).isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"Supabase snapshot error: {e}")


# ─────────────────────────────────────────────
# MAIN FETCH JOB
# ─────────────────────────────────────────────

def run_fetch_job():
    logger.info("Starting daily fetch job...")

    fb = fetch_facebook_metrics()
    ig = fetch_instagram_metrics()
    yt = fetch_youtube_metrics()

    if fb:
        upsert_summary("facebook", fb)
        upsert_posts(fb.get("posts", []))
        store_snapshot("facebook", "followers", fb.get("followers", 0))

    if ig:
        upsert_summary("instagram", ig)
        upsert_posts(ig.get("posts", []))
        store_snapshot("instagram", "followers", ig.get("followers", 0))

    if yt:
        upsert_summary("youtube", yt)
        upsert_posts(yt.get("posts", []))
        store_snapshot("youtube", "subscribers", yt.get("followers", 0))

    logger.info("Fetch job complete.")


# ─────────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────────

@app.route("/api/summary")
def api_summary():
    if not supabase:
        return jsonify({"error": "DB not configured"}), 500
    try:
        result = supabase.table("elements_social_summary").select("*").execute()
        data = {row["platform"]: row for row in result.data}
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/posts/<platform>")
def api_posts(platform):
    if not supabase:
        return jsonify({"error": "DB not configured"}), 500
    try:
        query = supabase.table("elements_social_posts").select("*")
        if platform != "all":
            query = query.eq("platform", platform)
        result = query.order("published_at", desc=True).limit(20).execute()
        return jsonify(result.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/snapshots/<platform>/<metric>")
def api_snapshots(platform, metric):
    if not supabase:
        return jsonify({"error": "DB not configured"}), 500
    try:
        result = supabase.table("elements_social_snapshots") \
            .select("*") \
            .eq("platform", platform) \
            .eq("metric_key", metric) \
            .order("captured_at", desc=False) \
            .limit(90) \
            .execute()
        return jsonify(result.data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    try:
        run_fetch_job()
        return jsonify({"status": "ok", "refreshed_at": datetime.now(IST).isoformat()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    return render_template("dashboard.html")


# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone=IST)
scheduler.add_job(run_fetch_job, "cron", hour=6, minute=0)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
