-- Rol de ESCRITURA ACOTADA para el formulario /contacto (2026-08-02).
--
-- Se ejecuta A MANO con `neondb_owner`, una sola vez, sustituyendo la
-- contraseña. NO se guarda aquí la contraseña real ni se sube a ningún sitio:
-- vive dentro de `DATABASE_URL_WRITE` en Vercel, igual que la de
-- `mma_app_readonly` vive dentro de `DATABASE_URL`.
--
-- LA IDEA: si esta cadena de conexión se filtrara, lo peor que puede hacer
-- quien la tenga es METER mensajes de contacto. No puede leer los que hay, ni
-- tocar una sola fila del resto de la base.

-- 1. El rol. Sin ningún atributo: ni superusuario, ni crear bases, ni roles,
--    ni saltarse RLS, ni heredar de nadie.
CREATE ROLE mma_app_contact WITH LOGIN PASSWORD 'CAMBIAR-POR-UNA-LARGA-Y-ALEATORIA'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

-- 2. Poder entrar al esquema. Nada más: USAGE no da acceso a ninguna tabla.
GRANT USAGE ON SCHEMA public TO mma_app_contact;

-- 3. El único privilegio de datos que tiene en toda la base.
--    OJO: INSERT y NO SELECT. Con SELECT podría leer los mensajes de los demás,
--    que es justo lo que no queremos que pueda hacer una credencial que vive en
--    una función pública de internet.
GRANT INSERT ON TABLE contact_messages TO mma_app_contact;

-- 4. `bigserial` necesita poder pedir el siguiente id. USAGE basta: no permite
--    leer el valor actual de la secuencia, solo avanzarla.
GRANT USAGE ON SEQUENCE contact_messages_id_seq TO mma_app_contact;

-- 4b. 🪤 LA TRAMPA QUE COSTO MEDIA HORA, Y QUE HABRIA REVENTADO EN PRODUCCION:
--     `INSERT ... RETURNING` EXIGE PRIVILEGIO SELECT.
--     En Postgres, devolver cualquier columna es una lectura, asi que con INSERT
--     a secas un `RETURNING id` falla con "permission denied for table
--     contact_messages" — el MISMO mensaje que si faltara el INSERT, que es lo
--     que despista. Comprobado a mano:
--         INSERT ... VALUES (...);              -> OK
--         INSERT ... VALUES (...) RETURNING id; -> permission denied
--     Por eso /api/contacto inserta SIN `RETURNING`: no necesita el id para
--     nada. La alternativa seria `GRANT SELECT (id) ON contact_messages`, pero
--     no hace falta abrir nada.

-- 5. COMPROBACIÓN, que es la parte que de verdad importa. Con el rol ya creado,
--    conectarse COMO ÉL y confirmar que:
--      INSERT INTO contact_messages (email, message) VALUES ('a@b.c', 'prueba'); -- OK
--      INSERT INTO contact_messages (email, message) VALUES ('a@b.c', 'x') RETURNING id; -- DENEGADO, y es correcto
--      SELECT * FROM contact_messages;   -- debe dar "permission denied"
--      SELECT * FROM fighters;           -- debe dar "permission denied"
--    Si alguna de las dos ultimas devuelve filas, el rol esta mal y hay que
--    revisar los GRANT por defecto del esquema antes de poner nada en Vercel.
