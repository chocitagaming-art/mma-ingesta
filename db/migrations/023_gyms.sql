-- 023_gyms.sql — Gimnasios de artes marciales (Cajón E). Fuente: OpenStreetMap
-- (Overpass), SOLO España, poblados por src/scrapers/gyms_osm.py (cron semanal
-- refresh-gyms.yml). Sustituye la consulta Overpass en runtime de /api/gyms por
-- una lectura rápida desde Neon (sin límite de 120, cache larga, listo para crecer
-- con otra fuente). `sports` guarda los tokens OSM CRUDOS (boxing, jiu-jitsu…); las
-- etiquetas en español y la descripción por plantilla se derivan en la app (gyms.ts).

CREATE TABLE IF NOT EXISTS gyms (
  osm_id        TEXT PRIMARY KEY,               -- 'node/123' | 'way/456' (tipo/id OSM)
  name          TEXT NOT NULL,
  lat           DOUBLE PRECISION NOT NULL,
  lon           DOUBLE PRECISION NOT NULL,
  sports        TEXT[] NOT NULL DEFAULT '{}',   -- tokens OSM crudos del tag `sport`
  address       TEXT,                           -- compuesta solo de addr:* reales
  city          TEXT,                           -- addr:city (para la plantilla de descripción)
  province      TEXT,                           -- addr:province si existe (opcional)
  website       TEXT,
  phone         TEXT,
  opening_hours TEXT,
  source        TEXT NOT NULL DEFAULT 'osm',
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Índice para la búsqueda por proximidad (filtro por bounding-box lat/lon).
CREATE INDEX IF NOT EXISTS idx_gyms_lat_lon ON gyms (lat, lon);
