-- 012_fase6.sql
-- Fase 6 (auditoría backend): stats por asalto (BE4). Todo aditivo e
-- idempotente (IF NOT EXISTS), como 004-011.
--
--   fight_stats_rounds : desglose POR ASALTO de las métricas core que
--                        fight_stats guarda sumadas ("summed across all
--                        per-round rows", ver 005). La página fight-details de
--                        ufcstats sirve la tabla "Per round" con una fila por
--                        asalto (thead "Round N" + tr de datos); el parser
--                        parse_fight_stats_rounds (src/scrapers/parsers/
--                        fights.py) persiste cada fila además del total.
--                        Escriben: backfill_results (cron diario, peleas
--                        recién terminadas) y backfill_round_stats (backfill
--                        histórico por lotes, workflow_dispatch).
--                        Upsert ON CONFLICT (fight_id, fighter_id, round) con
--                        COALESCE: un re-scrape que no parsea una métrica
--                        nunca borra un valor almacenado.
--
-- NO aplicada a producción todavía: aplicar antes del próximo cron de
-- refresh-upcoming (su paso backfill_results ya consulta esta tabla) y antes
-- de despachar backfill-round-stats.

CREATE TABLE IF NOT EXISTS fight_stats_rounds (
    id SERIAL PRIMARY KEY,
    fight_id INT NOT NULL REFERENCES fights(id) ON DELETE CASCADE,
    fighter_id INT NOT NULL REFERENCES fighters(id) ON DELETE CASCADE,
    round INT NOT NULL,
    sig_strikes_landed INT,
    sig_strikes_attempted INT,
    takedowns_landed INT,
    takedowns_attempted INT,
    submission_attempts INT,
    control_time_seconds INT,
    knockdowns INT,
    UNIQUE (fight_id, fighter_id, round)
);

-- Lecturas del frontend: rounds de una pelea (cubierto por la UNIQUE) e
-- histórico por luchador.
CREATE INDEX IF NOT EXISTS idx_fight_stats_rounds_fighter_id
    ON fight_stats_rounds(fighter_id);
