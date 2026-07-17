# Elements Senior Living — Social Dashboard

Daily-refreshed social media intelligence dashboard for Elements Senior Living.
Tracks YouTube, Facebook, and Instagram metrics with 90-day trend history.

## Stack
- Flask + APScheduler (daily cron 6 AM IST)
- Supabase Postgres (metric storage + history)
- Chart.js (trend charts)
- Railway (hosting)

---

## Setup

### 1. Supabase — Run migration
Open your Supabase project → SQL Editor → paste and run `supabase_migration.sql`

### 2. Push to GitHub
```bash
git init
git add .
git commit -m "init: elements social dashboard"
git remote add origin https://github.com/bm-aip/elements-social-dashboard.git
git push -u origin main
```

### 3. Deploy to Railway
- New Project → Deploy from GitHub → select `elements-social-dashboard`
- Add environment variables (see `.env.example`)

### 4. Set env vars in Railway
```
META_PAGE_ID             = 100728566307909
META_ACCESS_TOKEN        = <long-lived page token>
YOUTUBE_CLIENT_ID        = <from Google Cloud Console>
YOUTUBE_CLIENT_SECRET    = <from Google Cloud Console>
YOUTUBE_REFRESH_TOKEN    = <OAuth2 refresh token>
SUPABASE_URL             = <your supabase project URL>
SUPABASE_KEY             = <your supabase anon key>
```

### 5. First data load
After deploy, hit the **↻ Refresh** button on the dashboard to pull initial data.
Subsequent pulls happen automatically at 6 AM IST daily.

---

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/summary` | Latest summary per platform |
| `GET /api/posts/all` | All posts, last 20 per platform |
| `GET /api/posts/youtube` | YouTube posts only |
| `GET /api/posts/facebook` | Facebook posts only |
| `GET /api/posts/instagram` | Instagram posts only |
| `GET /api/snapshots/:platform/:metric` | 90-day history for trend charts |
| `POST /api/refresh` | Trigger manual data fetch |

---

## Notes
- YouTube quota: pulls top 20 videos by view count to stay within 10k daily quota
- Meta token: long-lived tokens expire in ~60 days — set a calendar reminder to rotate
- No auth: dashboard is public. Add Railway's built-in HTTP auth if needed.
