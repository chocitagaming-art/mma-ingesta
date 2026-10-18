-- 031_salon_video_recortes.sql
-- Rediseño del Salón de la Fama (18-oct-2026): el vídeo de cada pelea del Ala de
-- Combate, el encuadre de las fotos y el dato «¿esta foto viene recortada?».
-- Aditiva e idempotente (IF NOT EXISTS; constraints con DROP ... IF EXISTS + ADD),
-- como la 014. Aplicación MANUAL o vía el workflow seed-hall-of-fame.yml, que la
-- aplica antes de correr el seed y después mide las fotos.
--
-- Atómica: va entera en una transacción (ADD COLUMN, CREATE TABLE, COMMENT y GRANT
-- son transaccionales en Postgres). Si un ADD CONSTRAINT falla en una
-- re-ejecución (una fila editada a mano que viola la regla), el DROP de ese mismo
-- constraint se deshace con él y la tabla NO se queda sin el CHECK. Y con
-- lock_timeout: el ADD CONSTRAINT pide ACCESS EXCLUSIVE sobre hall_of_fame; si
-- tropieza con una transacción abierta, falla a los 5 s en vez de esperar sin
-- límite con los lectores de la web en cola detrás.
--
-- La web (mma-app, getHallOfFame) la lee con una reserva: si estas columnas o la
-- tabla photo_cutout aún no existen, el Salón se pinta igual, todo «enmarcado» y
-- sin vídeo. Eso NO quiere decir que dé igual el orden de despliegue:
--   1. merge a main de mma-ingesta (el workflow_dispatch corre el YAML de la rama
--      por defecto: antes del merge lanzaría el seed viejo);
--   2. seed-hall-of-fame.yml: esta migración + seed + medida de fotos;
--   3. SOLO ENTONCES desplegar la web que borra public/hof/charles-mask-lewis.webp
--      y art-davie.webp: hasta el seed, sus filas apuntan a esos ficheros y las
--      dos tarjetas darían 404;
--   4. POST /api/revalidate (el último paso del workflow ya lo hace, pero el tag
--      'hall-of-fame' solo existe con la web nueva: tras desplegarla, repetirlo).
-- Al revés (seed antes que web nueva) no se rompe nada: la web vieja pinta los
-- retratos 16:9 de ufc.com en su cuadrado sin photo_focus, solo un rato.

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ============================================================================
-- 1) EL VÍDEO DE LA PELEA (Ala de Combate)
-- ============================================================================
-- Guardamos el video id de YouTube (11 chars) y el front deriva el reproductor
-- youtube-nocookie, como en la 022, la 027 y la 029. NULL = no hay vídeo, y la
-- tarjeta conserva el botón que BUSCA la pelea en YouTube.
--
-- REGLA DEL DUEÑO: si UFC Español tiene la pelea COMPLETA, va la suya. Medido el
-- 18-oct: solo la tiene en 3 de las 11 (Jones–Gustafsson, Adesanya–Gastelum y
-- Zhang–Jędrzejczyk); de las otras 8 solo hay clips de menos de 5 minutos.
--
-- video_start / video_end: 4 de las 11 peleas viven DENTRO de un maratón (un
-- vídeo de 2 horas con varias peleas), y sin estos segundos el visitante caería
-- en la pelea equivocada. Salen de los capítulos del propio vídeo.
--
-- 🪤 SIN UNIQUE en video_id, a propósito: E2fUq7gT8z4 («GREATEST UFC Hall of
-- Fame Fights») sirve para DOS peleas (Coleman–Williams y Hughes–Trigg II),
-- cada una con su tramo.
ALTER TABLE hall_of_fame ADD COLUMN IF NOT EXISTS video_id TEXT;
ALTER TABLE hall_of_fame ADD COLUMN IF NOT EXISTS video_start INTEGER;
ALTER TABLE hall_of_fame ADD COLUMN IF NOT EXISTS video_end INTEGER;

-- El id tiene la forma de YouTube: 11 caracteres de [A-Za-z0-9_-]. Un id roto no
-- llega nunca a pintarse como un reproductor muerto.
ALTER TABLE hall_of_fame DROP CONSTRAINT IF EXISTS hall_of_fame_video_id_format;
ALTER TABLE hall_of_fame ADD CONSTRAINT hall_of_fame_video_id_format
    CHECK (video_id IS NULL OR video_id ~ '^[A-Za-z0-9_-]{11}$');

-- El inicio solo tiene sentido con vídeo, y no es negativo.
ALTER TABLE hall_of_fame DROP CONSTRAINT IF EXISTS hall_of_fame_video_start_valid;
ALTER TABLE hall_of_fame ADD CONSTRAINT hall_of_fame_video_start_valid
    CHECK (video_start IS NULL OR (video_id IS NOT NULL AND video_start >= 0));

-- El final solo tiene sentido con vídeo, y va DESPUÉS del inicio (o del segundo 0
-- si no hay inicio).
ALTER TABLE hall_of_fame DROP CONSTRAINT IF EXISTS hall_of_fame_video_end_valid;
ALTER TABLE hall_of_fame ADD CONSTRAINT hall_of_fame_video_end_valid
    CHECK (video_end IS NULL OR (video_id IS NOT NULL AND video_end > COALESCE(video_start, 0)));

-- ============================================================================
-- 2) EL ENCUADRE DE LA FOTO
-- ============================================================================
-- photo_focus: el punto de la CARA dentro de la foto, como 'X% Y%' (p. ej.
-- '57% 22%'). Hace falta para los retratos oficiales del Salón de ufc.com, que
-- son 16:9 con el cuerpo entero: metidos tal cual en el marco cuadrado, la cara
-- sale diminuta o cortada. La web lo usa como object-position y como origen del
-- zoom. NULL = encuadre por defecto (centrado).
ALTER TABLE hall_of_fame ADD COLUMN IF NOT EXISTS photo_focus TEXT;

-- Formato estricto: dos porcentajes enteros de 0 a 100 separados por un espacio.
-- Se pega tal cual en un estilo CSS, así que no se admite nada más.
ALTER TABLE hall_of_fame DROP CONSTRAINT IF EXISTS hall_of_fame_photo_focus_format;
ALTER TABLE hall_of_fame ADD CONSTRAINT hall_of_fame_photo_focus_format
    CHECK (photo_focus IS NULL OR photo_focus ~ '^(100|[1-9]?[0-9])% (100|[1-9]?[0-9])%$');

COMMENT ON COLUMN hall_of_fame.video_id IS
  'Video id de YouTube (11 chars) de la pelea COMPLETA (Ala de Combate). Regla del dueño: si UFC Español tiene la pelea completa, va la suya. Sin UNIQUE: un maratón sirve para varias peleas. NULL = sin vídeo; la web conserva el botón que busca en YouTube.';
COMMENT ON COLUMN hall_of_fame.video_start IS
  'Segundo en que empieza la pelea dentro del vídeo (maratones). NULL = desde el principio. Solo con video_id.';
COMMENT ON COLUMN hall_of_fame.video_end IS
  'Segundo en que termina la pelea dentro del vídeo (maratones). NULL = hasta el final. Solo con video_id y > video_start.';
COMMENT ON COLUMN hall_of_fame.photo_focus IS
  'Punto de la cara dentro de la foto, ''X% Y%'', para encuadrar en el marco cuadrado los retratos 16:9 de ufc.com. NULL = centrado.';

-- ============================================================================
-- 3) ¿LA FOTO VIENE RECORTADA? (tabla photo_cutout)
-- ============================================================================
-- Una tarjeta del Salón se pinta de dos maneras: «en relieve» si la foto viene
-- recortada (fondo transparente, el luchador sale del marco) o «enmarcada» si
-- trae fondo. No se puede adivinar por el dominio: medido el 18-oct, la regla
-- «ufc.com = recorte» fallaba con Dan Severn (foto de galería con fondo) y con
-- Forrest Griffin (foto local que SÍ es un recorte).
--
-- POR QUÉ UNA TABLA POR URL Y NO UNA COLUMNA EN hall_of_fame. El dato va pegado a
-- la IMAGEN, no al miembro: 50 de los 72 huecos de foto del Salón salen de
-- fighters.headshot_url, que los crons de enrich cambian sin avisar. Con la tabla,
-- si la URL cambia el cruce no encuentra nada y la tarjeta cae a «enmarcada», que
-- es la opción segura. Y sirve tal cual para las esquinas del Ala de Combate y, el
-- día que haga falta, para la página de Luchadores.
--
-- 🪤 LA CLAVE ES LA URL EXACTA QUE GUARDA LA BASE. 643 headshot_url llevan el
-- texto '&amp;' literal, y el navegador pide esa misma cadena: si alguien la
-- «limpia» antes de guardarla aquí, el cruce falla en silencio.
--
-- La escribe src/scrapers/measure_photo_cutout.py (paso del workflow
-- seed-hall-of-fame.yml): mide las que faltan o tienen más de 30 días.
CREATE TABLE IF NOT EXISTS photo_cutout (
    -- La URL tal cual la guarda la base (hall_of_fame.photo_url o
    -- fighters.headshot_url). Las rutas locales ('/fighters/…') van tal cual: el
    -- script las pide a https://mmastatus.app + ruta.
    url          TEXT PRIMARY KEY,
    -- Recortada = RGBA, alfa 0 en las dos esquinas de arriba y ≥ 90 % de píxeles
    -- transparentes en el borde de arriba más los laterales (el de abajo no
    -- cuenta: los hombros lo tocan). Una medida fallida (sin width) NO pisa el
    -- veredicto anterior, salvo un 404/410 (la foto ya no existe), que lo deja
    -- en FALSE. Una URL que nunca se pudo medir nace FALSE (enmarcada).
    is_cutout    BOOLEAN NOT NULL,
    width        INTEGER,
    height       INTEGER,
    -- El código HTTP del ÚLTIMO intento, haya salido bien o no: así nos
    -- enteramos de una foto que ufc.com ha borrado (p. ej. la de Gerbasi se
    -- llama 'DO-NOT-USE-…'), y el script reintenta lo que no sea un 200.
    http_status  INTEGER,
    -- Cuándo se midió con éxito (o se vio que la foto ya no existe). Un fallo
    -- pasajero no lo mueve, para que la fila siga «caducada» y se reintente.
    checked_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE photo_cutout IS
    'Si cada imagen (por su URL EXACTA, &amp; incluido) viene recortada con fondo transparente. La escribe measure_photo_cutout.py; la lee el Salón de la Fama para elegir entre foto en relieve o enmarcada.';

-- La web lee el Salón con `mma_app_readonly`. Sin este GRANT, el LEFT JOIN
-- reventaría con "permission denied" (la web tiene reserva, pero se quedaría
-- todo enmarcado para siempre sin que nadie supiera por qué).
GRANT SELECT ON TABLE photo_cutout TO mma_app_readonly;

COMMIT;
