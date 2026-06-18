-- 003_rankings_espn.sql
-- Adds the columns the rankings scraper / frontend contract needs on top of the
-- base `rankings` table from 001_initial_schema.sql.
--   is_champion  : TRUE for the divisional champion (rank_position = 0).
--   fighter_name : the athlete name exactly as scraped (ALWAYS filled, even when
--                  fighter_id is NULL, so the frontend can render unmatched fighters).
--   rank_change  : weekly movement indicator. +N = up N, -N = down N,
--                  NULL = no change, 999 = NEW (re-entered / not previously ranked).
ALTER TABLE rankings ADD COLUMN IF NOT EXISTS is_champion BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE rankings ADD COLUMN IF NOT EXISTS fighter_name TEXT;
ALTER TABLE rankings ADD COLUMN IF NOT EXISTS rank_change INTEGER;
