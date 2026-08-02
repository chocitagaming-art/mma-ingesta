-- 025 (2026-08-02, /contacto): buzón del formulario de contacto de la web.
--
-- PROBLEMA: desde el 1-ago la web se conecta a Neon como `mma_app_readonly`,
-- que SOLO puede hacer SELECT. Eso es deliberado y no se toca. Pero el dueño
-- quiere un formulario de contacto, y un formulario escribe.
--
-- DECISIÓN DEL DUEÑO (2-ago), entre tres opciones: conexión de escritura APARTE
-- en vez de un servicio externo de correo o un simple `mailto:`. Razón: el
-- histórico se queda en su base de datos y no aparece una dependencia nueva.
--
-- DISEÑO — el privilegio es de UN SOLO SENTIDO, y por construcción:
--   1. La web NO usa este pool para nada más. `DATABASE_URL` sigue apuntando a
--      `mma_app_readonly` y sirve TODAS las lecturas del sitio; solo la ruta
--      /api/contacto abre el segundo pool (`DATABASE_URL_WRITE`).
--   2. El rol `mma_app_contact` tiene INSERT en ESTA tabla y nada más. En
--      particular NO tiene SELECT ni sobre ella: si alguien roba esa cadena de
--      conexión, no puede leer ni un mensaje ajeno, ni un luchador, ni nada.
--      El rol se crea en `db/roles/mma_app_contact.sql`, no aquí, porque lleva
--      contraseña y este fichero va a un repo público.
--   3. NO se guarda la IP, ni en claro ni con hash. El límite de peticiones ya
--      frena el abuso y no hace falta un dato personal más para leer un correo.
--      Lo que se guarda es lo que el visitante escribe a propósito.
--   4. `handled` es para el dueño: marcar lo ya contestado sin borrarlo.
--
-- ⚠️ SI ALGÚN DÍA SE AMPLÍA LO QUE ESTA TABLA GUARDA, la página /privacidad de
-- mma-app ES PARTE DEL CAMBIO. Ahí se enumera exactamente esto.
--
-- 🪤 Y QUIEN ESCRIBA CONTRA ESTA TABLA: `INSERT ... RETURNING` NO FUNCIONA con
-- el rol `mma_app_contact`. Devolver una columna es leerla, así que RETURNING
-- exige SELECT — y falla con "permission denied for table contact_messages",
-- el mismo mensaje que si faltara el INSERT. Insertar sin RETURNING.

CREATE TABLE IF NOT EXISTS contact_messages (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    -- Opcional a propósito: para contestar basta el correo, y pedir menos
    -- datos es menos que custodiar.
    name        text,
    email       text        NOT NULL,
    message     text        NOT NULL,
    -- Bandeja de entrada, no bitácora: marcar en vez de borrar.
    handled     boolean     NOT NULL DEFAULT false,
    -- Los topes viven TAMBIÉN aquí y no solo en el formulario: la validación
    -- del cliente es una comodidad, la de la API es la puerta, y esta es la
    -- última red por si alguna de las dos se afloja al refactorizar.
    CONSTRAINT contact_messages_email_len   CHECK (char_length(email) BETWEEN 5 AND 160),
    CONSTRAINT contact_messages_name_len    CHECK (name IS NULL OR char_length(name) <= 80),
    CONSTRAINT contact_messages_message_len CHECK (char_length(message) BETWEEN 10 AND 2000)
);

-- Para la única consulta que se va a hacer de verdad: "¿qué tengo sin leer?".
CREATE INDEX IF NOT EXISTS contact_messages_pendientes_idx
    ON contact_messages (created_at DESC)
    WHERE NOT handled;

COMMENT ON TABLE contact_messages IS
    'Mensajes del formulario /contacto. Escribe SOLO el rol mma_app_contact '
    '(INSERT, sin SELECT). La web general se conecta como mma_app_readonly.';
