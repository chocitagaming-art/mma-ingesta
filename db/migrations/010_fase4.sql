-- 010_fase4.sql
-- Fase 4 (auditoría backend): bonos post-evento, peleas de título, bio extendida
-- y peleas canceladas. Todo aditivo e idempotente (IF NOT EXISTS), como 004-009.
--
--   fight_bonuses      : bonos oficiales UFC ($50k) scrapeados de la sección
--                        "Bonus awards" de la Wikipedia inglesa por
--                        src/scrapers/wiki_bonuses.py.
--                          - FOTN (Fight of the Night)      -> lleva fight_id
--                            (la pelea premiada), fighter_id NULL.
--                          - POTN (Performance of the Night) -> lleva fighter_id
--                            (un registro por luchador premiado), fight_id NULL.
--   fights.is_title_fight : el bout view de ufc.com ya anuncia "Title Bout" en
--                        .c-listing-fight__class-text; upcoming_events.py lo
--                        detectaba para scheduled_rounds=5 pero lo descartaba.
--                        Ahora se persiste (BE9b).
--   fights.status      : NULL = pelea normal; 'cancelled' = pelea caída de la
--                        cartelera. La reconciliación de upcoming_events deja de
--                        borrar+reinsertar: marca cancelled lo que desaparece y
--                        reactiva (NULL) lo que reaparece (BE7). El frontend
--                        filtra status = 'cancelled' donde cuente peleas.
--   fighters.birth_place / octagon_debut : campos "Place of Birth" y "Octagon
--                        Debut" de la ficha de atleta de ufc.com que
--                        enrich_photos_ufc.py ya descarga (BE5). Se rellenan
--                        aditivamente (COALESCE, nunca pisan un valor).

-- ---------------------------------------------------------------- fight_bonuses

CREATE TABLE IF NOT EXISTS fight_bonuses (
    id SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    fight_id INTEGER REFERENCES fights(id) ON DELETE CASCADE,      -- FOTN
    fighter_id INTEGER REFERENCES fighters(id) ON DELETE CASCADE,  -- POTN
    bonus_type TEXT NOT NULL CHECK (bonus_type IN ('FOTN', 'POTN')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (event_id, bonus_type, fight_id, fighter_id)
);

-- La UNIQUE de arriba trata los NULL como distintos (semántica estándar de
-- Postgres < 15), y cada fila real lleva SIEMPRE un NULL (FOTN sin fighter_id,
-- POTN sin fight_id), así que por sí sola no impediría duplicados. Estos dos
-- índices únicos parciales cubren las dos formas reales; el INSERT del scraper
-- usa ON CONFLICT DO NOTHING sin target, que dispara contra cualquiera de ellos.
CREATE UNIQUE INDEX IF NOT EXISTS idx_fight_bonuses_fotn
    ON fight_bonuses (event_id, bonus_type, fight_id)
    WHERE fight_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_fight_bonuses_potn
    ON fight_bonuses (event_id, bonus_type, fighter_id)
    WHERE fighter_id IS NOT NULL;

-- Lecturas del frontend: bonos de un evento / de un luchador.
CREATE INDEX IF NOT EXISTS idx_fight_bonuses_event_id ON fight_bonuses(event_id);
CREATE INDEX IF NOT EXISTS idx_fight_bonuses_fighter_id ON fight_bonuses(fighter_id);

-- ----------------------------------------------------------------------- fights

ALTER TABLE fights ADD COLUMN IF NOT EXISTS is_title_fight BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE fights ADD COLUMN IF NOT EXISTS status TEXT;  -- NULL = normal; 'cancelled'

-- --------------------------------------------------------------------- fighters

ALTER TABLE fighters ADD COLUMN IF NOT EXISTS birth_place TEXT;    -- "Chone, Ecuador"
ALTER TABLE fighters ADD COLUMN IF NOT EXISTS octagon_debut DATE;  -- "Apr. 25, 2015" parseado
