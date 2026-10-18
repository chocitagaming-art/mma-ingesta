-- R6 del rediseño (17/18-oct-2026): el Instagram y el X de cada luchador, para
-- pintarlos como iconos en su ficha, debajo del apodo y el récord.
--
-- DE DÓNDE SALEN. De la página del atleta en ufc.com, que el cron
-- enrich-athlete-stats YA baja (a diario las carteleras próximas, el día 1 la
-- tabla entera): cero peticiones nuevas. ufc.com no pinta esos iconos en el
-- HTML; los mete en el bloque de datos estructurados
--   <script type="application/ld+json"> → @graph → mainEntity (Person) → sameAs
-- y SOLO de ahí se leen (src/scrapers/ufc_social.py).
--
-- 🪤 LA TRAMPA, medida el 18-oct en las 20 páginas de muestra: el pie de TODAS
-- las páginas de ufc.com enlaza las cuentas de la propia UFC
-- (instagram.com/ufc/, twitter.com/ufc). Buscar enlaces por toda la página le
-- asignaría @ufc a los 2.892 luchadores. Por eso el parseo no mira ni un <a>.
--
-- FORMA GUARDADA, normalizada (y estos CHECK son la red de seguridad):
--   · instagram_url = https://www.instagram.com/<handle en minúsculas>/
--   · x_url         = https://x.com/<handle>        (twitter.com → x.com)
-- sin parámetros (?ref=…, ?hl=…), sin @ y sin espacios. ufc.com los sirve
-- sucios: «twitter.com/@navajostirling», «facebook.com/Unavailable  »,
-- «instagram.com/TheNotoriousMMA». Lo que no cuadra con un perfil, no se guarda.
--
-- REGLA DE ESCRITURA (decisión del dueño, 18-oct, «ok a todo»):
--   · página con el bloque JSON-LD del Person → ufc.com manda: si lista la red,
--     se guarda la suya; si ya NO la lista, se BORRA (NULL). Así la ficha nunca
--     enlaza a una cuenta que ufc.com ya no reconoce.
--   · página SIN ese bloque → no se toca nada (no es «no tiene redes», es «no
--     lo sabemos»).
--   · la guarda anti-homónimos de siempre: si el nombre de la página no cuadra,
--     no se escribe.
-- NULL = sin red conocida, y la ficha no pinta ese icono.
--
-- Facebook NO: el dueño pidió solo Instagram y X.
--
-- Lectura de la web: fighters ya es legible por mma_app_readonly con un GRANT
-- de TABLA (comprobado en solo lectura el 18-oct: has_table_privilege = true y
-- role_table_grants = SELECT), así que las columnas nuevas le llegan sin GRANT.
--
-- Idempotente: se puede aplicar dos veces. El workflow enrich-athlete-stats la
-- aplica en un lanzamiento A MANO sin dry-run (nunca desde el cron, que arranca
-- a horas imprevisibles), y SOLO si aún faltan las columnas o los CHECK (así no
-- toma un candado de tabla por nada).
--
-- 🪤 EL FRENO. «Se borra lo que ufc.com deja de listar» significa que un cambio
-- de plantilla en ufc.com borraría las redes de TODOS en un solo barrido. Por
-- eso los borrados se juntan y se aplican al FINAL del run, y no se aplican (el
-- job sale en rojo, socials_brake=1) si el run tiene pinta de cambio en ufc.com.
-- Ver THE BRAKE en src/scrapers/enrich_athlete_stats.py.

-- Si alguien tiene fighters cogida (una consulta larga), mejor fallar en rojo a
-- los 10 s que dejar la web esperando detrás del ALTER.
SET lock_timeout = '10s';

ALTER TABLE fighters ADD COLUMN IF NOT EXISTS instagram_url TEXT;
ALTER TABLE fighters ADD COLUMN IF NOT EXISTS x_url TEXT;

ALTER TABLE fighters DROP CONSTRAINT IF EXISTS fighters_instagram_url_format;
ALTER TABLE fighters ADD CONSTRAINT fighters_instagram_url_format CHECK (
  instagram_url IS NULL
  OR instagram_url ~ '^https://www\.instagram\.com/[a-z0-9._]{1,30}/$'
);

ALTER TABLE fighters DROP CONSTRAINT IF EXISTS fighters_x_url_format;
ALTER TABLE fighters ADD CONSTRAINT fighters_x_url_format CHECK (
  x_url IS NULL
  OR x_url ~ '^https://x\.com/[A-Za-z0-9_]{1,15}$'
);

COMMENT ON COLUMN fighters.instagram_url IS
  'Perfil de Instagram del luchador tal como lo publica ufc.com (JSON-LD Person.sameAs de su pagina de atleta, NUNCA el pie de la pagina, que enlaza a @ufc). Normalizado: https://www.instagram.com/<handle en minusculas>/. NULL = ufc.com no lo lista (se borra si deja de listarlo) o aun sin scrapear. Escribe enrich_athlete_stats.py.';

COMMENT ON COLUMN fighters.x_url IS
  'Perfil de X (antes Twitter) del luchador tal como lo publica ufc.com (JSON-LD Person.sameAs, twitter.com pasa a x.com). Normalizado: https://x.com/<handle>. NULL = ufc.com no lo lista (se borra si deja de listarlo) o aun sin scrapear. Escribe enrich_athlete_stats.py.';
