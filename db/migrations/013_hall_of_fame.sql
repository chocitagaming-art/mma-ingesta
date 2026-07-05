-- 013_hall_of_fame.sql
-- Salón de la Fama de UFC (feature /salon-de-la-fama). Todo aditivo e
-- idempotente (IF NOT EXISTS), como 004-012.
--
--   hall_of_fame : un registro por inductee de las 4 alas del UFC Hall of Fame.
--                    - wing: 'pioneer' (debut antes del 2000-11-17), 'modern'
--                      (debut on/after), 'contributor' (no competidores:
--                      ejecutivos, árbitros, comentaristas…), 'fight' (peleas
--                      históricas).
--                    - display_name: nombre a mostrar SIEMPRE. Para 'fight' es
--                      "A vs. B I"; para el resto, el nombre del inductee.
--                    - inductee_year: año de inducción.
--                    - fighter_id: enlace a nuestra ficha si el inductee es un
--                      luchador nuestro (NULL en contributor y en pioneros sin
--                      ficha). Para 'fight' se usan fighter_a_id/fighter_b_id
--                      (las dos esquinas) y fighter_id queda NULL.
--                    - subtitle: contexto opcional (para 'fight', el evento,
--                      p.ej. "UFC 165"; para el resto NULL).
--                    - photo_url: foto propia opcional (NULL = usar la de la
--                      ficha enlazada, o el placeholder de iniciales).
--                    - sort_order: orden dentro del ala (cronológico).
--                  Datos curados (lista fija conocida) que seedea de forma
--                  idempotente src/scrapers/seed_hall_of_fame.py (ON CONFLICT
--                  por (wing, display_name)), con linkado fuzzy a fighters
--                  reusando matching.IDENTITY_THRESHOLD (0.92).
--                  Migración de aplicación MANUAL (no hay runner automático).

-- ------------------------------------------------------------- hall_of_fame

CREATE TABLE IF NOT EXISTS hall_of_fame (
    id SERIAL PRIMARY KEY,
    wing TEXT NOT NULL CHECK (wing IN ('pioneer', 'modern', 'contributor', 'fight')),
    display_name TEXT NOT NULL,
    inductee_year INTEGER,
    fighter_id INTEGER REFERENCES fighters(id),    -- inductee luchador (modern/pioneer)
    fighter_a_id INTEGER REFERENCES fighters(id),  -- 'fight': esquina A
    fighter_b_id INTEGER REFERENCES fighters(id),  -- 'fight': esquina B
    subtitle TEXT,                                 -- 'fight': evento (p.ej. "UFC 165")
    photo_url TEXT,                                -- foto propia opcional
    sort_order INTEGER NOT NULL DEFAULT 0,         -- orden cronológico dentro del ala
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (wing, display_name)                    -- clave natural para upsert idempotente
);

-- Lecturas del frontend: por ala (la pestaña activa) y por luchador (badge en su ficha).
CREATE INDEX IF NOT EXISTS idx_hall_of_fame_wing ON hall_of_fame(wing);
CREATE INDEX IF NOT EXISTS idx_hall_of_fame_fighter_id ON hall_of_fame(fighter_id);
