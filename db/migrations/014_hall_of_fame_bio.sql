-- 014_hall_of_fame_bio.sql
-- Biografía corta para el panel expandible del Salón (alas Contributor y Fight).
-- Aditivo e idempotente (IF NOT EXISTS), como 004-013. Aplicación MANUAL o vía
-- el workflow seed-hall-of-fame.yml (que la aplica antes de correr el seed).
--
--   bio : prosa corta (2-3 frases) que se muestra al expandir la card en el
--         Salón. subtitle sigue siendo la línea corta (rol / evento); bio es el
--         texto largo. La puebla src/scrapers/seed_hall_of_fame.py (idempotente).

ALTER TABLE hall_of_fame ADD COLUMN IF NOT EXISTS bio TEXT;
