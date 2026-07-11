-- 015 (2026-07-11, S2-E): Fighter Facts + Q&A de ufc.com, YA TRADUCIDOS al
-- español por la ingesta (Claude) antes de almacenarse. Primeras columnas
-- JSONB del esquema:
--   fighter_facts: ["Profesional desde 2013", ...]        (array de strings)
--   fighter_qa:    [{"q": "¿...?", "a": "..."}, ...]      (array de objetos)
-- Aditiva e idempotente (IF NOT EXISTS); no toca datos existentes.

ALTER TABLE fighters ADD COLUMN IF NOT EXISTS fighter_facts JSONB;
ALTER TABLE fighters ADD COLUMN IF NOT EXISTS fighter_qa JSONB;
