-- Los DOS actos de la víspera en vídeo: el careo y el pesaje. Y, con ellos, el
-- título real de cada uno, que es lo que impide que la página mienta.
--
-- Hermana de la 022 (faceoff_video_id) y de la 027 (live_video_id +
-- live_video_title), y con la misma forma: guardamos el video id de 11 chars y
-- el front deriva el reproductor. NULL = no hay vídeo, y entonces NO SE PINTA
-- NADA. Igual que en la 022 y en la 027: un hueco vacío es preferible a enseñar
-- un vídeo que no es el de esta velada.
--
-- ⚠️ POR QUÉ HACEN FALTA LOS TÍTULOS, y esto no es teoría: pasó.
-- Hasta hoy el rótulo «Careo oficial» está escrito A MANO en el JSX
-- (mma-app/src/app/eventos/[id]/page.tsx, el h2 de la sección del careo). Una
-- frase fija encima de un vídeo variable afirma algo que nadie ha comprobado.
-- El 16-sep-2026 el casador metió en el evento 1090 («Crypto.com UFC 331: Van
-- vs. Pantoja 2») el vídeo 0xTX8Aut0VY, un short de 17 segundos titulado «what
-- are these faceoffs saying?! #ufc331», y la web lo anunció como CAREO OFICIAL.
-- El rótulo era nuestro; el vídeo, no.
-- Con el título real guardado, la página dice lo que el vídeo dice ser y no
-- puede volver a mentir. Es EXACTAMENTE el razonamiento que la 027 ya hizo para
-- live_video_title («el rótulo de pantalla sale del TÍTULO REAL del vídeo y no
-- de una frase escrita a mano»), aplicado ahora al careo y al pesaje.
--
-- Se guarda el título en vez de pedirlo a la API en cada render por los dos
-- motivos de la 027: no gastar cuota en cada visita, y que la página siga
-- entendiéndose si YouTube no contesta.
ALTER TABLE events ADD COLUMN IF NOT EXISTS faceoff_video_title TEXT;

-- EL PESAJE, que es OTRO ACTO y por eso es OTRA COLUMNA, no un segundo uso de
-- la del careo.
--
-- Son dos cosas distintas de la misma semana y el aficionado las distingue:
--   · el careo puede ser el de la rueda de prensa (miércoles o jueves) o el
--     ceremonial (viernes por la tarde, con público);
--   · el pesaje es la MAÑANA DEL VIERNES, en sala pequeña y sin público, y es
--     donde se ve si alguien no da el peso.
-- Meterlos en una sola columna obligaría a elegir cuál de los dos se enseña y
-- perdería el otro. Son dos bloques independientes en la página: si hay uno y
-- no el otro, se pinta el que haya.
--
-- Ejemplo real verificado el 18-sep-2026 (evento 1090, velada del 19):
--   careo  · MQLCbgV5rhc · «#CryptoCom #UFC331: Careos Conferencia de Prensa»
--            · canal ufcespanol (oficial) · 3:15
--   pesaje · enkyfSnB0r0 · «UFC 331: Official Weigh-Ins»
--            · canal TheMacLife · 17:20
--
-- ⚠️ Y AQUÍ LA ADVERTENCIA HONESTA: el vídeo de pesaje disponible es de
-- TheMacLife, un medio tercero acreditado, NO del canal de la UFC. El ACTO es
-- oficial; el CANAL no lo es. Un vídeo de tercero puede borrarse, volverse
-- privado o caerse por derechos de un día para otro, y no nos avisa nadie. Por
-- eso la columna admite NULL y por eso el bloque NO SE PINTA si está vacía: la
-- página tiene que aguantar perder el pesaje sin romperse ni dejar un
-- reproductor muerto. Cuando la UFC publique el suyo, se sustituye el id.
ALTER TABLE events ADD COLUMN IF NOT EXISTS weighin_video_id TEXT;

-- El título del pesaje, por lo mismo que el del careo: el rótulo lo pone el
-- vídeo, no nosotros. Y aquí gana el doble, porque es lo único que le dice al
-- visitante de qué canal viene lo que está viendo.
ALTER TABLE events ADD COLUMN IF NOT EXISTS weighin_video_title TEXT;

COMMENT ON COLUMN events.faceoff_video_title IS
  'Titulo REAL del video de careo, tal cual lo publica YouTube. Es el rotulo que pinta la pagina: sustituye a la frase fija Careo oficial escrita a mano en el JSX, que el 16-sep-2026 anuncio como careo oficial un short de 17 segundos. NULL = no se pinta rotulo propio.';

COMMENT ON COLUMN events.weighin_video_id IS
  'Video id (11 chars) del PESAJE, acto distinto del careo: el careo es rueda de prensa o ceremonial, el pesaje es el viernes por la manana. NULL = no hay VIDEO de pesaje y el reproductor NO SE PINTA. La tabla de pesos es independiente y sigue pintandose si hay filas. Puede venir de un medio tercero acreditado (TheMacLife), no siempre del canal de la UFC.';

COMMENT ON COLUMN events.weighin_video_title IS
  'Titulo REAL del video de pesaje, tal cual lo publica YouTube. Mismo motivo que faceoff_video_title y que live_video_title (migracion 027): el rotulo lo pone el video y no una frase nuestra. NULL = no se pinta rotulo propio.';

-- 🪤 NOTA DE NUMERACIÓN, para quien venga siguiendo un rastro.
-- La 028 dejó escrito —en su comentario y, peor, en el COMMENT de
-- events.tier_override, que vive DENTRO de la base— que la regla de
-- public.event_tier() se cambiaría «en la migración 029». No era una reserva del
-- número, era una forma de decir «en la siguiente que escribas». Esta 029 es el
-- vídeo del pesaje; el cambio de tier irá en la 030 o en la que toque.
