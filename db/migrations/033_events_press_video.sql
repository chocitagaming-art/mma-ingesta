-- La RUEDA DE PRENSA de la semana de la velada, en vídeo. Y, con ella, su
-- título real, que es lo que impide que la página mienta.
--
-- Hermana de la 022 (faceoff_video_id), de la 027 (live_video_id +
-- live_video_title) y de la 029 (el título del careo y la pareja del pesaje), y
-- con la misma forma: guardamos el video id de 11 chars y el front deriva el
-- reproductor. NULL = no hay rueda, y entonces NO SE PINTA NADA. Igual que en
-- las otras tres: un hueco vacío es preferible a enseñar un vídeo que no es el
-- de esta velada.
--
-- El rótulo lo pone el vídeo, no el JSX: por eso viaja el título. Es el
-- razonamiento de la 027 («el rótulo de pantalla sale del TÍTULO REAL del vídeo
-- y no de una frase escrita a mano») y de la 029, que lo pagó: el 16-sep-2026
-- una frase fija anunció como CAREO OFICIAL un short de 17 segundos.
--
-- LA RUEDA ES OTRO ACTO, y por eso es OTRA COLUMNA y no un uso más de la del
-- careo. El mismo argumento que separó el pesaje en la 029:
--   · la rueda de prensa es la CONFERENCIA entera, miércoles o jueves: media
--     hora o más de preguntas y respuestas;
--   · el careo son unos minutos frente a frente, y a menudo SALE DE ESA MISMA
--     rueda. El del UFC 333 que ya está en la base lo dice en el título:
--     hVvIw8K754c · «#UFC333 Volkanovski vs Evloev Careos de la Conferencia»
--     · canal ufcespanol · 72 s.
-- En una sola columna habría que elegir entre la rueda y el careo, y se perdería
-- uno. Y hay una razón más, que la 029 no tenía: faceoff_video_id la rellena el
-- cron (match_faceoffs) y es FIRST-WRITER-WINS. Una rueda escrita ahí a mano
-- le cerraría la puerta al careo para siempre. Columnas propias = bloques
-- independientes en la ficha: si hay uno y no el otro, se pinta el que haya.
--
-- SE RELLENA SOLO A MANO, con scripts/fijar_videos_evento.py --rueda (título
-- pedido al oembed, aviso de short, simulación por defecto). Ningún cron la
-- escribe ni la borra: lo decidió el dueño el 22-oct-2026, porque elegir QUÉ
-- rueda se enseña (en español, aunque no sea la del canal oficial) es suyo.
--
-- Ejemplo real verificado el 22-oct-2026 con el oembed y /watch (evento 1093,
-- UFC 333, velada del 24):
--   elegida · g3h-jwpkxJU · «UFC 333: CONFERENCIA DE PRENSA COMPLETA EN ESPAÑOL»
--             · canal «MMA Español» (@MMAenEspañol2k24) · 1.402 s (23:22)
--   oficial · DHLdj5yFWmw · «UFC 333: Pre-Fight Press Conference»
--             · canal UFC · 2.025 s (33:45), en inglés. NO es la elegida.
--
-- ⚠️ Y AQUÍ LA ADVERTENCIA HONESTA, la misma que la 029 dejó para el pesaje de
-- TheMacLife, y aquí pesa más: la rueda elegida para el UFC 333 es de un canal
-- TERCERO y NO OFICIAL, una resubida. El ACTO es oficial; el CANAL no lo es, y
-- ni siquiera es un medio que haya grabado el acto. Un vídeo así puede
-- borrarse, volverse privado o caerse por derechos de un día para otro, y no
-- nos avisa nadie. Por eso la columna admite NULL y por eso el bloque NO SE
-- PINTA si está vacía: la ficha tiene que aguantar perder la rueda sin romperse
-- ni dejar un reproductor muerto. Si se cae, se sustituye el id (la oficial es
-- la de arriba) o se deja a NULL.
--
-- Lectura de la web: events ya es legible por mma_app_readonly con un GRANT de
-- TABLA (comprobado en solo lectura el 22-oct: role_table_grants = SELECT y
-- has_table_privilege = true), así que las columnas nuevas le llegan sin GRANT.
--
-- Aditiva e idempotente: dos columnas que admiten NULL y sin DEFAULT, que en
-- PostgreSQL es solo catálogo (no reescribe la tabla), y se puede aplicar dos
-- veces. La copia diaria (mma-docs, backup-db.yml) no necesita nada: su puerta
-- de cordura compara FILAS por tabla, y esto no cambia ninguna.

-- La ficha y la portada leen events en cada visita: si alguien la tiene cogida,
-- mejor fallar en rojo a los 10 s que dejar la web esperando detrás del ALTER.
SET lock_timeout = '10s';

ALTER TABLE events ADD COLUMN IF NOT EXISTS press_video_id TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS press_video_title TEXT;

COMMENT ON COLUMN events.press_video_id IS
  'Video id (11 chars) de la RUEDA DE PRENSA, acto distinto del careo: la rueda es la conferencia entera (miercoles o jueves), el careo son unos minutos frente a frente. NULL = no hay rueda y el reproductor NO SE PINTA. Se rellena SOLO a mano con scripts/fijar_videos_evento.py --rueda; ningun cron la toca. Puede venir de un canal tercero no oficial (la del UFC 333, g3h-jwpkxJU, lo es): puede caerse sin aviso.';

COMMENT ON COLUMN events.press_video_title IS
  'Titulo REAL del video de la rueda de prensa, tal cual lo publica YouTube. Mismo motivo que faceoff_video_title y weighin_video_title (migracion 029) y que live_video_title (027): el rotulo lo pone el video y no una frase nuestra, y es lo unico que le dice al visitante de que canal viene. NULL = no se pinta rotulo propio.';
