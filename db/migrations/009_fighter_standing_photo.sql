-- 009: standing full-body photo from ufc.com event bout views (redesign round A).
-- ufc.com serves a different athlete photo on event pages (Drupal style
-- "event_fight_card_upper_body_of_standing_athlete": full standing body, PNG alpha,
-- ~185x600). Captured daily by the upcoming-events scraper and by the one-off
-- backfill over 2026 event pages. Newest scraped value wins (UFC refreshes the
-- photo after each fight), unlike full_body_url where existing values are kept.

ALTER TABLE fighters ADD COLUMN IF NOT EXISTS standing_body_url TEXT;
