-- 005_fight_stats_breakdown.sql
-- Significant-strike breakdown by TARGET (head/body/leg) and POSITION
-- (distance/clinch/ground), for the ufc.com-style silhouette + position split
-- on the fighter card (spec items #45/#46).
--
-- These come from the 2nd stats table on each ufcstats fight-details page
-- ("Significant Strikes"), summed across all per-round rows. The same backfill
-- pass that populates them also FIXES #44: the old scraper read only round 1 of
-- each fight, so every existing fight_stats row (sig strikes, takedowns, control,
-- knockdowns, submissions) was undercounted. Re-scraping rewrites those too.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. All nullable (very old fights / fights
-- without a per-target table keep NULL). Values are landed / attempted counts.

ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_head_landed INTEGER;
ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_head_attempted INTEGER;
ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_body_landed INTEGER;
ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_body_attempted INTEGER;
ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_leg_landed INTEGER;
ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_leg_attempted INTEGER;
ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_distance_landed INTEGER;
ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_distance_attempted INTEGER;
ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_clinch_landed INTEGER;
ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_clinch_attempted INTEGER;
ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_ground_landed INTEGER;
ALTER TABLE fight_stats ADD COLUMN IF NOT EXISTS sig_str_ground_attempted INTEGER;
