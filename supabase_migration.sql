-- Run this in Supabase SQL Editor before deploying

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

CREATE TABLE IF NOT EXISTS elements_social_snapshots (
    id SERIAL PRIMARY KEY,
    platform TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    metric_value NUMERIC,
    captured_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast snapshot queries
CREATE INDEX IF NOT EXISTS idx_snapshots_platform_metric
    ON elements_social_snapshots (platform, metric_key, captured_at);

-- Index for post queries
CREATE INDEX IF NOT EXISTS idx_posts_platform_published
    ON elements_social_posts (platform, published_at DESC);
