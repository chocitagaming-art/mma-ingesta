-- 006_fight_video_url.sql
-- Optional CURATED link to a fight's full video (#43). NULL by default; when a
-- value is present the fight detail page links straight to it, otherwise the UI
-- falls back to a YouTube search URL (buildFightVideoSearchUrl, #42). Curation
-- is manual/ad-hoc, so this column just enables it without requiring it.
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE fights ADD COLUMN IF NOT EXISTS video_url TEXT;
