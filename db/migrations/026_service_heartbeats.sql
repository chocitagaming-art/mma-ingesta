-- 026 (2026-08-02): el latido de los servicios que viven FUERA de esta base.
--
-- POR QUE EXISTE. El 1-ago a las 23:12Z el microservicio de prediccion entro en
-- bucle de caida y estuvo NUEVE HORAS muerto. El panel /estado -- que es lo que
-- el dueno mira a diario -- tiene 22 comprobaciones y ninguna lo veia, asi que
-- `estado-guardian` salio en verde toda la noche. El aviso llego solo por los
-- Issues de GitHub, que es el canal que el propio proyecto ya documento como
-- fragil (un Issue viejo abierto amordaza los siguientes).
--
-- POR QUE UNA TABLA Y NO UNA SONDA DESDE EL PANEL. La tentacion es que
-- /api/estado le haga un curl al servicio. No sirve, y esta medido:
--   · el servicio responde en 0,17 s cuando esta caliente, pero un arranque en
--     frio de Render tarda ~43 s (medido en su propio log: 22:09:46 -> 22:10:29);
--   · una sonda con presupuesto corto no distingue "dormido" de "muerto", asi
--     que o miente o se cuelga;
--   · y colgar /api/estado es peor que no comprobar nada, porque esa ruta la
--     lee el guardian cada hora.
-- Ademas contradiria la filosofia que el propio panel declara por escrito: "se
-- mide el DATO, no si el cron dijo que habia terminado bien". Un latido ES el
-- dato: alguien hablo con el servicio y le contesto.
--
-- QUIEN ESCRIBE AQUI. `keepalive-prediction.yml`, despues de que su curl a
-- /health devuelva 200. Ese workflow ya espera hasta 70 s por intento, asi que
-- absorbe el arranque en frio sin falsos negativos.
--
-- POR QUE `service` ES LA CLAVE Y NO HAY HISTORIAL. Esto no es una serie
-- temporal: la unica pregunta es "cuando fue la ultima vez que contesto". Una
-- fila por servicio, actualizada en su sitio. Si algun dia hace falta el
-- historico, se anade una tabla aparte y esta se queda como esta.

CREATE TABLE IF NOT EXISTS service_heartbeats (
    -- Nombre corto y estable del servicio. 'prediction' es el microservicio de
    -- Render; si manana hay otro, se anade una fila, no una columna.
    service     TEXT PRIMARY KEY,
    -- Cuando contesto por ultima vez. Es lo unico que el panel necesita.
    last_ok_at  TIMESTAMPTZ NOT NULL,
    -- Contexto libre para el humano que mire la fila a mano (version, quien lo
    -- escribio). NO lo interpreta ningun codigo: si algun dia hace falta un
    -- campo estructurado, se anade una columna.
    detail      TEXT
);

COMMENT ON TABLE service_heartbeats IS
    'Ultima vez que cada servicio externo respondio. Lo escribe el keep-alive; lo lee el panel /estado.';

-- La web lee el panel con `mma_app_readonly`. Sin este GRANT la comprobacion
-- nueva reventaria en produccion con "permission denied" y el panel entero se
-- caeria -- que es peor que el punto ciego que viene a tapar.
GRANT SELECT ON TABLE service_heartbeats TO mma_app_readonly;

-- 🪤 A PROPOSITO: NO se le da SELECT a `mma_prediction_ro`. El servicio de
-- prediccion no tiene por que leer su propio latido, y ese rol enumera sus
-- tablas una a una justamente para que nadie herede permisos por descuido.
