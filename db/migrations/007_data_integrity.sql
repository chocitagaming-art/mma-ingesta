-- 007_data_integrity.sql
-- Phase 5 (MMA_PLAN_MEJORAS_2026-06-27.md): DB-level integrity constraints.
-- Idempotent and transactional: safe to re-apply. A read-only pre-flight on
-- 2026-06-27 confirmed 0 violating rows for every constraint below; the rankings
-- natural-key duplicates (snapshot 2026-06-24) are removed BEFORE this migration.

BEGIN;

-- 1. fights: protect the model training label.
--    Plain '<>' is intentional: it yields NULL (=> CHECK passes) when either
--    corner is NULL, so upcoming bouts with *_name fallbacks are still allowed;
--    it only rejects a non-null self-fight.
ALTER TABLE fights DROP CONSTRAINT IF EXISTS fights_red_ne_blue;
ALTER TABLE fights ADD CONSTRAINT fights_red_ne_blue
    CHECK (fighter_red_id <> fighter_blue_id);

ALTER TABLE fights DROP CONSTRAINT IF EXISTS fights_winner_is_participant;
ALTER TABLE fights ADD CONSTRAINT fights_winner_is_participant
    CHECK (winner_id IS NULL OR winner_id = fighter_red_id OR winner_id = fighter_blue_id);

-- 2. events: a real natural key. idx_events_source only covers source_id IS NOT NULL,
--    so the ~783 historical ufcstats events (source_id NULL) had no dedup guard and
--    upsert_event's ON CONFLICT DO NOTHING never fired.
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_name_date_promotion_key;
ALTER TABLE events ADD CONSTRAINT events_name_date_promotion_key
    UNIQUE (name, event_date, promotion_id);

-- 3. rankings: one fighter per (division, rank slot, date) + non-negative rank.
ALTER TABLE rankings DROP CONSTRAINT IF EXISTS rankings_slot_key;
ALTER TABLE rankings ADD CONSTRAINT rankings_slot_key
    UNIQUE (promotion_id, division, rank_position, snapshot_date);

ALTER TABLE rankings DROP CONSTRAINT IF EXISTS rankings_rank_position_nonneg;
ALTER TABLE rankings ADD CONSTRAINT rankings_rank_position_nonneg
    CHECK (rank_position >= 0);

-- 4. FK hygiene: a fight's stats are owned by the fight (cascade on delete);
--    index the two FKs that merges/reconcile scan but Postgres never indexed.
ALTER TABLE fight_stats DROP CONSTRAINT IF EXISTS fight_stats_fight_id_fkey;
ALTER TABLE fight_stats ADD CONSTRAINT fight_stats_fight_id_fkey
    FOREIGN KEY (fight_id) REFERENCES fights(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_fight_stats_fighter_id ON fight_stats(fighter_id);
CREATE INDEX IF NOT EXISTS idx_fights_winner_id ON fights(winner_id);

COMMIT;
