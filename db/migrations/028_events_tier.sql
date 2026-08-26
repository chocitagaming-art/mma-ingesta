-- El TIPO de velada: una columna DERIVADA, que no escribe nadie.
--
-- EL PROBLEMA, tal cual pasó. El 26-ago-2026 el "Road To UFC: Maheshate vs.
-- Flowers" (id 1094, viernes 28, 2 combates, sin sede, sin póster y sin cuotas)
-- desplazó al UFC Fight Night del sábado (id 1065, 13 combates) en la portada,
-- en /eventos, en /en-vivo, en /ufc-hoy, en /estado y en /directo. Los dos
-- tienen promotion_id = 1, así que LA PROMOTORA NO LOS DISTINGUE: lo único que
-- los separa es cómo se llaman. Todas las consultas de "¿cuál es el próximo?"
-- ordenaban por fecha y nada más, y el viernes va antes que el sábado.
--
-- POR QUÉ GENERADA Y NO UNA ETIQUETA QUE ESCRIBA LA INGESTA. El dato ya está en
-- la tabla por partida doble: source_id es el slug literal de ufc.com
-- ('road-to-ufc-season-5-semifinals' frente a 'ufc-fight-night-august-29-2026'),
-- y `name` empieza por la serie que declara ufc.com. Guardar además una etiqueta
-- sería copiar en una columna lo que ya está en otras dos, y las copias se
-- desincronizan: hay 8 sentencias que escriben en events desde 5 ficheros, y una
-- de ellas (backfill_standing_photos.py) ESTAMPA source_id en filas históricas
-- que no lo tenían. Con GENERATED, ese UPDATE recalcula el tier gratis.
-- Y no hay backfill que correr: la columna nace completa y correcta.
--
-- LA DIRECCIÓN EN QUE FALLA, elegida a conciencia. Lista negra, y lo que no
-- reconoce cae en 'unknown' -> DESTACABLE. Un formato nuevo que no clasifiquemos
-- se VE en la portada (el fallo de hoy: molesto, visible y de un renglón) y
-- nunca se esconde. Una lista blanca ("solo es principal si el slug empieza por
-- ufc-") tiraría de la portada a "Noche UFC: Silva vs. Delgado" y al
-- "Crypto.com UFC 331" (slug cryptocom-ufc-331, por el patrocinador). Los dos
-- existen HOY en la tabla.
--
-- REPARTO VERIFICADO sobre las 792 filas, ejecutando el CASE el 26-ago-2026:
--   fight_night 427 | numbered 327 | tuf_finale 28 | unknown 8 | road_to_ufc 2
-- Los 8 'unknown' son veladas UFC completas y TODAS pasadas (Ultimate Ultimate
-- '95 y '96, Ultimate Japan, Ultimate Brazil, Ortiz vs Shamrock 3, Silva vs
-- Irvin, UFC Macao, UFC Freedom 250): la prueba de que 'unknown' tiene que caer
-- del lado principal.

BEGIN;

-- LA REGLA, y vive SOLO aquí.
-- NO es STRICT a propósito: 761 de las 792 filas tienen source_id NULL (el
-- histórico de ufcstats) y con RETURNS NULL ON NULL INPUT se quedarían sin
-- clasificar. IMMUTABLE es obligatorio para usarla en una columna generada.
CREATE OR REPLACE FUNCTION public.event_tier(p_source_id text, p_name text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $funcion$
  SELECT CASE
    -- Torneo asiático de cantera. El que provocó todo esto.
    WHEN t ~* 'road[ _-]*to[ _-]*ufc'                         THEN 'road_to_ufc'
    -- Dana White's Contender Series. Aún no hay ninguno en la tabla; entrará
    -- clasificado el día que llegue, sin tocar nada.
    WHEN t ~* 'contender[ _-]*series|dwcs|dana[ _-]*white'    THEN 'dwcs'
    -- El programa TUF, NUNCA su Finale: las 28 "... Finale" son carteles UFC de
    -- verdad y siguen siendo principales. El orden de estas dos ramas importa.
    WHEN t ~* 'ultimate[ _-]*fighter' AND t !~* 'final'       THEN 'tuf_series'
    WHEN t ~* 'ultimate[ _-]*fighter'                         THEN 'tuf_finale'
    WHEN t ~* '(^| )ufc[ -]?[0-9]{1,4}([ :.]|$)'              THEN 'numbered'
    WHEN t ~* 'fight[ _-]*night|noche[ _-]*ufc|ufc[ _-]*on[ _-]|ufc[ _-]*live'
                                                              THEN 'fight_night'
    ELSE 'unknown'
  END
  FROM (SELECT coalesce(p_source_id,'') || ' ' || coalesce(p_name,'') AS t) AS s
$funcion$;

COMMENT ON FUNCTION public.event_tier(text, text) IS
  'Deriva el tipo de velada del slug de ufc.com y del nombre. Lista negra con unknown->principal: lo que no reconoce sale destacable, o sea falla ensenando de mas y nunca escondiendo una velada real. Unica definicion de la regla en todo el proyecto.';

-- LA VÁLVULA DE ESCAPE. Una columna generada NO se puede UPDATEar, y la noche de
-- una velada no se aplica una migración. Si la regla se equivocara con un evento
-- concreto, esto lo arregla en cinco segundos y sin desplegar nada:
--   UPDATE events SET tier_override = 'fight_night' WHERE id = 1065;
-- Debe estar a NULL en todas las filas en régimen normal. Es el ÚNICO camino que
-- le queda a este diseño para volver a desincronizarse, y por eso lleva alarma
-- en data_quality_checks.py desde el mismo commit.
ALTER TABLE events ADD COLUMN IF NOT EXISTS tier_override text;

ALTER TABLE events DROP CONSTRAINT IF EXISTS events_tier_override_check;
ALTER TABLE events ADD CONSTRAINT events_tier_override_check CHECK (
  tier_override IS NULL OR tier_override IN
    ('numbered','fight_night','tuf_finale','road_to_ufc','dwcs','tuf_series','unknown')
);

-- LA COLUMNA. STORED y no VIRTUAL: se puede indexar y se puede mirar con un
-- SELECT normal cuando algo va raro un sábado por la noche.
-- No hace falta CHECK sobre `tier`: como es generada, nadie puede escribirla.
--
-- ⚠️ ADD COLUMN IF NOT EXISTS no cambia la expresión de una columna que ya
-- exista. CAMBIAR la regla más adelante se hace en su propia migración 029, y
-- estas dos líneas son la plantilla exacta:
--     CREATE OR REPLACE FUNCTION public.event_tier(...) ...;
--     ALTER TABLE events ALTER COLUMN tier
--       SET EXPRESSION AS (COALESCE(tier_override, public.event_tier(source_id, name)));
-- SET EXPRESSION (PG17+; Neon corre 18.6, verificado) reescribe la tabla y
-- RECALCULA las 792 filas. Sin él, cambiar el cuerpo de la función dejaría los
-- valores viejos ahí quietos, que es justo lo que este diseño existe para evitar.
ALTER TABLE events ADD COLUMN IF NOT EXISTS tier text
  GENERATED ALWAYS AS (COALESCE(tier_override, public.event_tier(source_id, name))) STORED;

COMMENT ON COLUMN events.tier IS
  'Tipo de velada. DERIVADA de source_id y name por public.event_tier(): no se escribe, no se PUEDE escribir y no puede quedar desincronizada. road_to_ufc|dwcs|tuf_series NO pueden ser el evento destacado del sitio, pero SIGUEN teniendo pagina, saliendo en /eventos, en el buscador y en el sitemap. Filtro: tier NOT IN (esos tres).';

COMMENT ON COLUMN events.tier_override IS
  'Excepcion manual para tier, NULL en regimen normal. Solo para una urgencia en directo; lo permanente se arregla cambiando public.event_tier() en la migracion 029.';

-- No hace falta índice: son 792 filas y estas consultas ya hacen recorrido
-- secuencial hoy. Si algún día sobraran filas:
--   CREATE INDEX idx_events_proximos_principales ON events (event_date, id)
--     WHERE tier NOT IN ('road_to_ufc','dwcs','tuf_series') AND status = 'upcoming';

-- LAS CUATRO GUARDAS. Van DENTRO de la transacción: si alguna salta, el ALTER se
-- deshace entero y la base se queda EXACTAMENTE como estaba.
DO $guardas$
DECLARE
  v_sec int; v_lista text; v_sabado text; v_intruso text;
BEGIN
  SELECT count(*), string_agg(id || ' ' || name, ' | ' ORDER BY id)
    INTO v_sec, v_lista
    FROM events WHERE tier IN ('road_to_ufc','dwcs','tuf_series');

  -- 1. Ninguna "... Finale" puede caer del lado secundario: son 28 carteles UFC
  --    completos y borrarlos del bloque "Último evento" sería peor que el bug.
  IF EXISTS (SELECT 1 FROM events
              WHERE tier IN ('road_to_ufc','dwcs','tuf_series') AND name ~* 'final') THEN
    RAISE EXCEPTION 'ABORTADA: la regla degrada una velada Finale. Revisa event_tier().';
  END IF;

  -- 2. Tiene que quedar al menos un evento futuro destacable, o la portada se
  --    queda muda: sin hero y sin un solo error en los registros.
  IF NOT EXISTS (SELECT 1 FROM events
                  WHERE status = 'upcoming'
                    AND tier NOT IN ('road_to_ufc','dwcs','tuf_series')) THEN
    RAISE EXCEPTION 'ABORTADA: no queda ni un evento futuro destacable.';
  END IF;

  -- 3. La velada del sábado 29 (id 1065) tiene que ser principal. Es la que se
  --    retransmite en directo dentro de tres días.
  SELECT tier INTO v_sabado FROM events WHERE id = 1065;
  IF v_sabado IS DISTINCT FROM 'fight_night' THEN
    RAISE EXCEPTION 'ABORTADA: el evento 1065 quedo como % y tiene que ser fight_night.',
      coalesce(v_sabado, '(no existe)');
  END IF;

  -- 4. Y el 1094 tiene que ser secundario. Comprobar solo el lado bueno dejaría
  --    pasar una regla que no clasifica nada.
  SELECT tier INTO v_intruso FROM events WHERE id = 1094;
  IF v_intruso IS DISTINCT FROM 'road_to_ufc' THEN
    RAISE EXCEPTION 'ABORTADA: el evento 1094 quedo como % y tiene que ser road_to_ufc.',
      coalesce(v_intruso, '(no existe)');
  END IF;

  RAISE NOTICE 'OK - % principales, % secundarias: %',
    (SELECT count(*) FROM events) - v_sec, v_sec, coalesce(v_lista,'(ninguna)');
END
$guardas$;

COMMIT;
