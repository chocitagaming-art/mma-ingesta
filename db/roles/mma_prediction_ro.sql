-- Rol de SOLO LECTURA para el microservicio de prediccion en Render (2026-08-02).
--
-- Se ejecuta A MANO con `neondb_owner`, sustituyendo la contrasena. NO se
-- guarda aqui la contrasena real: vive dentro de la variable DATABASE_URL del
-- servicio en Render, igual que la de `mma_app_readonly` vive en Vercel.
--
-- POR QUE EXISTE ESTE ROL, Y NO REUTILIZA EL DE LA WEB
-- ----------------------------------------------------
-- El servicio compartia `mma_app_readonly` con la web. El 1-ago sobre las
-- 23:00Z se roto esa contrasena; se actualizo en Vercel y NO en Render, y el
-- servicio entro en bucle de caida durante mas de nueve horas:
--
--   psycopg2.OperationalError: ... password authentication failed for user
--   'mma_app_readonly'
--   ERROR:    Application startup failed. Exiting.        (uvicorn sale con 3)
--
-- Nada de lo que el dueno mira a diario lo enseñaba, porque el panel /estado
-- no comprueba Render. Y no se pudo arreglar copiando el valor de Vercel: las
-- variables sensibles salen como `[SENSITIVE]` en `vercel env pull`, y Neon no
-- guarda las contrasenas en claro. Rotar la de `mma_app_readonly` habria
-- obligado a redesplegar la web entera (un cambio de variable en Vercel no se
-- aplica sin build nuevo) seis dias antes de una velada.
--
-- Con un rol propio, el servicio se arregla sin tocar la web ni una sola vez, y
-- la proxima rotacion de cualquiera de los dos ya no puede tumbar al otro. Es
-- el mismo patron que `mma_app_contact`: una credencial por consumidor, con lo
-- justo para su trabajo.
--
-- QUE PUEDE HACER SI SE FILTRA: leer datos publicos de MMA (los mismos que la
-- web ya enseña a cualquiera). No puede escribir nada, no puede leer
-- `contact_messages`, y no puede tocar el esquema.

-- 1. El rol. Sin ningun atributo: ni superusuario, ni crear bases, ni roles,
--    ni saltarse RLS, ni heredar de nadie.
CREATE ROLE mma_prediction_ro WITH LOGIN PASSWORD 'CAMBIAR-POR-UNA-LARGA-Y-ALEATORIA'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

-- 2. Poder entrar al esquema. USAGE por si solo no da acceso a ninguna tabla.
GRANT USAGE ON SCHEMA public TO mma_prediction_ro;

-- 3. Lectura sobre las tablas que el servicio consulta de verdad. La lista sale
--    de seguir los imports de service.py -> api.py -> features/: la consulta
--    base une fights, events, fighters y fight_stats, y las features de
--    contexto miran rankings y el historial. Se conceden una a una y NO con
--    `ALL TABLES IN SCHEMA public`, porque ese atajo es exactamente el que
--    metio `contact_messages` dentro del alcance de `mma_app_readonly`.
GRANT SELECT ON TABLE
    events,
    fight_bonuses,
    fight_history_espn,
    fight_scorecards,
    fight_stats,
    fight_stats_rounds,
    fighters,
    fights,
    rankings
TO mma_prediction_ro;

-- 4. 🪤 LA TRAMPA HERMANA DE LA DE `mma_app_contact`: alli, un `INSERT ...
--    RETURNING` fallaba con "permission denied" porque devolver una columna es
--    LEERLA y el rol no tenia SELECT. Aqui la simetrica: un rol de solo lectura
--    NO necesita permiso sobre las secuencias, pero SI necesita que el pool
--    pueda abrir la conexion — y `init_pool` la abre en el arranque, antes de
--    servir nada. Si este GRANT se queda corto, el sintoma NO es un 500 en
--    /predict: es que el contenedor no arranca y Render devuelve 502 sin un
--    solo byte, que es justo lo que costo nueve horas de diagnostico.
--    Por eso, tras crear el rol, se comprueba conectando y haciendo un SELECT
--    real de cada tabla antes de tocar Render.

-- 5. Nada de privilegios por defecto sobre tablas futuras: una tabla nueva la
--    tiene que conceder alguien a mano, a conciencia. Es lo contrario de lo que
--    paso con `contact_messages`.
