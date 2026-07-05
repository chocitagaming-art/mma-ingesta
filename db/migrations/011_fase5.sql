-- 011_fase5.sql
-- Fase 5 (auditoría backend): pesajes oficiales, horarios por sección de
-- cartelera y árbitro/tarjetas de los jueces. Todo aditivo e idempotente
-- (IF NOT EXISTS), como 004-010.
--
--   weigh_ins            : resultados del pesaje oficial que ufc.com publica
--                          la víspera de cada evento como artículo de noticias
--                          ("Official Weigh-In Results ..."), scrapeados por
--                          src/scrapers/weigh_ins.py. Un registro por
--                          (pelea, luchador) con el peso en libras tal cual lo
--                          publica UFC (185.5) y el flag de missed weight.
--   events.early_prelims_time / prelims_time :
--                          la página de evento de ufc.com anuncia cada sección
--                          en bloques c-event-fight-card-broadcaster__container
--                          (título "Main Card"/"Prelims"/"Early Prelims" + hora
--                          en __time[data-timestamp]). start_time (004) ya
--                          guarda el main card; estas dos columnas completan
--                          las otras secciones (BE6). Política no-pisar-con-
--                          NULL: un re-scrape sin horas nunca borra las
--                          almacenadas (COALESCE en upsert_event_meta).
--   fights.referee       : "Referee:" de la página fight-details de
--                          ufcstats.com (fights.source_id =
--                          '/fight-details/<hash>'), scrapeado por
--                          src/scrapers/fight_officials.py (BE8).
--   fight_scorecards     : en decisiones, la misma página lista las tarjetas
--                          de los 3 jueces ("<juez> 28 - 29."). Un registro
--                          por (pelea, juez); red_score/blue_score siguen las
--                          esquinas de la fila de fights.
--
-- NO aplicada a producción todavía: aplicar antes del próximo cron de
-- refresh-upcoming / refresh-weighins / refresh-officials.

-- -------------------------------------------------------------------- weigh_ins

CREATE TABLE IF NOT EXISTS weigh_ins (
    id SERIAL PRIMARY KEY,
    fight_id INTEGER NOT NULL REFERENCES fights(id) ON DELETE CASCADE,
    fighter_id INTEGER NOT NULL REFERENCES fighters(id) ON DELETE CASCADE,
    weight_lbs NUMERIC(5,1),
    missed_weight BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (fight_id, fighter_id)
);

-- Lecturas del frontend: pesajes de una pelea (cubierto por la UNIQUE) y
-- histórico de pesajes de un luchador.
CREATE INDEX IF NOT EXISTS idx_weigh_ins_fighter_id ON weigh_ins(fighter_id);

-- ----------------------------------------------------------------------- events

ALTER TABLE events ADD COLUMN IF NOT EXISTS early_prelims_time TIMESTAMPTZ;
ALTER TABLE events ADD COLUMN IF NOT EXISTS prelims_time TIMESTAMPTZ;

-- ----------------------------------------------------------------------- fights

ALTER TABLE fights ADD COLUMN IF NOT EXISTS referee TEXT;

-- -------------------------------------------------------------- fight_scorecards

CREATE TABLE IF NOT EXISTS fight_scorecards (
    id SERIAL PRIMARY KEY,
    fight_id INTEGER NOT NULL REFERENCES fights(id) ON DELETE CASCADE,
    judge_name TEXT NOT NULL,
    red_score INTEGER,
    blue_score INTEGER,
    UNIQUE (fight_id, judge_name)
);
