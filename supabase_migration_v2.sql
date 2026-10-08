-- Run this in Supabase SQL Editor AFTER the original supabase_migration.sql.
-- Adds the failure-visibility and video-count columns. Idempotent.

ALTER TABLE elements_social_summary
    ADD COLUMN IF NOT EXISTS fetch_status    TEXT,
    ADD COLUMN IF NOT EXISTS fetch_error     TEXT,
    ADD COLUMN IF NOT EXISTS last_success_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS video_count     INTEGER DEFAULT 0;

-- Note: one snapshot per platform/metric/day is enforced in app code
-- (store_snapshot updates the day's row instead of inserting a duplicate),
-- because supabase-py cannot target an expression index for upsert.
