-- 008_fighter_bio_extras.sql
-- Phase 2 (MMA STATUS redesign): extra bio data from ufc.com athlete pages,
-- the same HTML enrich_photos_ufc.py already downloads for headshots.
--   full_body_url : official full-body photo (athlete_bio_full_body style) from
--                   img.hero-profile__image. NOT constructible a priori — it must
--                   be extracted from the served HTML.
--   leg_reach_cm  : "Leg reach" bio field, converted inches -> cm (e.g. 41.50 -> 105.4).
--   trains_at     : "Trains at" bio field (gym, e.g. 'Millennia MMA, Rancho, CA').
-- All nullable; filled additively (COALESCE, never overwrites) by
-- src/scrapers/enrich_fullbody.py. Idempotent: ADD COLUMN IF NOT EXISTS.
ALTER TABLE fighters ADD COLUMN IF NOT EXISTS full_body_url TEXT;
ALTER TABLE fighters ADD COLUMN IF NOT EXISTS leg_reach_cm NUMERIC;
ALTER TABLE fighters ADD COLUMN IF NOT EXISTS trains_at TEXT;
