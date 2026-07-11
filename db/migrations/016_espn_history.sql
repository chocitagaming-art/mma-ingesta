-- 016 (2026-07-11, S3-G): historial COMPLETO de peleas vía ESPN (Bellator y
-- promociones regionales) para el historial de la ficha.
--
-- DISEÑO: tabla APARTE (fight_history_espn), NO filas nuevas en `fights`.
-- Así las 4 guardas críticas del plan se cumplen POR CONSTRUCCIÓN:
--   1. El modelo ML (features/db.py lee `fights`) no ve estas peleas.
--   2. Las stats/rachas/división de la ficha (queries sobre `fights`) siguen
--      siendo solo UFC; el frontend fusiona esta tabla ÚNICAMENTE al pintar
--      la tabla del historial.
--   3. Los scripts de mantenimiento (cleanup_orphan_fights, cleanup_non_espn,
--      consolidate_fighters, recover_winners, dedupe...) operan sobre `fights`
--      y no pueden tocar estas filas.
--   4. Sin duplicados UFC: el import SALTA la liga UFC de ESPN (l:3321);
--      las peleas UFC viven solo en `fights` (ufcstats/ufc.com).
--
-- fighters.espn_id: id de atleta ESPN también para luchadores originados en
-- ufcstats (los source='espn' ya lo llevan en source_id; este pase lo
-- generaliza). Índice único parcial: un atleta ESPN no puede apuntar a dos
-- filas de fighters.
--
-- fighters.espn_history_checked_at: sello del último barrido de historial
-- (evita re-pedir semanalmente la carrera completa de ~2.8k luchadores cuyo
-- historial ya se importó y no cambia).
--
-- Aditiva e idempotente (IF NOT EXISTS); no toca datos existentes.

ALTER TABLE fighters ADD COLUMN IF NOT EXISTS espn_id TEXT;
ALTER TABLE fighters ADD COLUMN IF NOT EXISTS espn_history_checked_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS fighters_espn_id_key
    ON fighters (espn_id)
    WHERE espn_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS fight_history_espn (
    id SERIAL PRIMARY KEY,
    fighter_id INTEGER NOT NULL REFERENCES fighters(id) ON DELETE CASCADE,
    -- competitionId del uid de ESPN (s:3301~l:...~e:...~c:<este>): clave de
    -- idempotencia por luchador. Si dos luchadores NUESTROS pelearon entre sí
    -- fuera de la UFC hay una fila por cada perspectiva (es un historial por
    -- luchador, no un registro canónico de peleas).
    espn_competition_id TEXT NOT NULL,
    espn_event_id TEXT,
    league_id TEXT,
    -- Etiqueta legible para el badge: 'Bellator', 'CFFC', 'Tech-Krep FC'...
    -- (la liga UFC nunca se almacena aquí).
    promotion TEXT,
    event_name TEXT,
    event_date DATE,
    opponent_name TEXT,
    opponent_espn_id TEXT,
    opponent_fighter_id INTEGER REFERENCES fighters(id) ON DELETE SET NULL,
    result TEXT CHECK (result IN ('win', 'loss', 'draw', 'nc')),
    method TEXT,
    end_round INTEGER,
    end_time TEXT,
    is_title_fight BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (fighter_id, espn_competition_id)
);

CREATE INDEX IF NOT EXISTS fight_history_espn_fighter_idx
    ON fight_history_espn (fighter_id, event_date DESC);
