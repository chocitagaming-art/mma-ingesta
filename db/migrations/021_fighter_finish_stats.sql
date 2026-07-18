-- 1A: desglose de ACABADOS de carrera desde ufc.com (athlete stats).
--
-- Los tiles del hero de la ficha (Victorias KO / Victorias sumisión /
-- Finalizaciones en 1er asalto) se calculaban SOLO sobre peleas UFC (tabla
-- `fights`), así que un luchador cuyos acabados son pre-UFC/regionales mostraba
-- 0/0/0 aunque ufc.com liste los totales de carrera (ej. Anna Melisano: la
-- ficha decía 0/0/0, ufc.com dice 2/1/2). Estas columnas guardan las cifras
-- OFICIALES de ufc.com (totales de CARRERA), consistentes con el record W-L-D
-- almacenado (que también es total de carrera). NULL = luchador sin ficha
-- ufc.com o aún sin scrapear -> la app cae al cálculo UFC-only (fallback).
--
-- Escritura: enrich_athlete_stats.py (new-value-wins; los totales solo crecen).
ALTER TABLE fighters ADD COLUMN IF NOT EXISTS wins_by_ko INTEGER;
ALTER TABLE fighters ADD COLUMN IF NOT EXISTS wins_by_submission INTEGER;
ALTER TABLE fighters ADD COLUMN IF NOT EXISTS first_round_finishes INTEGER;
