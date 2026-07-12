-- 020 (F7 Tanda 4): busqueda web insensible a acentos.
--
-- Hoy searchFighters / searchEvents hacen `name ILIKE '%q%'`, asi que buscar
-- "jiri" NO encuentra "Jiří Procházka" (y viceversa). Habilitamos el fold de
-- acentos en la BD para ambos lados de la comparacion.
--
-- unaccent() es STABLE (depende del search_path para resolver el diccionario por
-- defecto), por lo que NO se puede indexar directamente. El patron estandar es un
-- wrapper IMMUTABLE que fija el diccionario por nombre cualificado; asi se pueden
-- crear indices funcionales sobre f_unaccent(col).
--
-- pg_trgm + indice GIN sobre f_unaccent(name): un ILIKE '%x%' (comodin inicial)
-- no puede usar un btree, pero SI un GIN trigram -> la busqueda foldeada sigue
-- yendo por indice en vez de seq scan.
--
-- Todo idempotente (IF NOT EXISTS / OR REPLACE). Aditiva: no toca datos.

CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE OR REPLACE FUNCTION f_unaccent(text)
RETURNS text
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $func$
    SELECT public.unaccent('public.unaccent', $1)
$func$;

CREATE INDEX IF NOT EXISTS idx_fighters_name_unaccent_trgm
    ON fighters USING gin (f_unaccent(name) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_events_name_unaccent_trgm
    ON events USING gin (f_unaccent(name) gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_events_location_unaccent_trgm
    ON events USING gin (f_unaccent(location) gin_trgm_ops);
