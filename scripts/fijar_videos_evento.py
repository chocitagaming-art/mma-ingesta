# -*- coding: utf-8 -*-
"""Fija A MANO los videos de la vispera de un evento: el careo y el pesaje.

POR QUE EXISTE ESTO. `set_event_faceoff_video` (repositories/events.py) es
FIRST-WRITER-WINS a proposito: solo escribe mientras la columna esta a NULL, asi
el cron de `match_faceoffs` no puede pisar un careo bueno con uno peor. El precio
de esa garantia es que cuando el cron mete uno MALO no hay forma soportada de
corregirlo: la columna ya no esta a NULL y el propio cron no volvera a tocarla.

Y paso. El 16-sep-2026 el casador metio en el evento 1090 ("Crypto.com UFC 331:
Van vs. Pantoja 2") el video 0xTX8Aut0VY, un short de 17 segundos titulado "what
are these faceoffs saying?! #ufc331", y la web lo anuncio como careo oficial. La
unica salida fue un UPDATE suelto a pelo contra produccion. Eso es lo que este
script viene a sustituir: la correccion manual existe igual, pero con red.

LAS TRES REDES, y son las que justifican el fichero:

1. **El titulo no se teclea, se PIDE.** Dado un video id, el script pregunta su
   titulo al oembed publico de YouTube y guarda ESE. Un operador cansado que
   escribe "Careo oficial" a mano esta afirmando algo que nadie ha comprobado;
   es la mentira que la migracion 029 vino a cerrar. Aqui no hay ninguna opcion
   para escribir un titulo a mano, y es deliberado.

2. **El video tiene que EXISTIR.** El id se valida de forma (11 caracteres de
   [A-Za-z0-9_-]) y contra el oembed. Un id invalido no da error en la web: deja
   un reproductor muerto, que es peor porque nadie se entera.

3. **Avisa de los shorts.** Si el video dura menos de 75 segundos (el mismo
   umbral que `MIN_DURATION_SECONDS` de mma-app/src/lib/youtube.ts) lo dice bien
   visible ANTES de escribir. 75 s es exactamente la frontera que el short de 17
   segundos habria cruzado.

DECISION QUE NO ES OBVIA: el id y su titulo VIAJAN JUNTOS. Al escribir un id
nuevo se escribe tambien su columna `_title`, y si el titulo no se pudo resolver
se escribe NULL. Nunca se deja el titulo viejo al lado de un id nuevo: eso es
precisamente el rotulo que miente, solo que ahora con nuestra firma.

EL DIRECTO (live_video_id + live_video_title, migracion 027). Es la via MANUAL,
la red de seguridad del automatico de la web. Tres palancas, y solo una a la vez:

  --directo <id o URL>  fija el directo a mano. Mismas redes que careo y pesaje
                        (titulo por oembed, que exista) MENOS el aviso de short:
                        un directo en curso o programado da lengthSeconds 0 y
                        el aviso saltaria SIEMPRE, que es lo mismo que no avisar.
                        Acepta la URL pegada tal cual, porque es lo que se tiene
                        a mano en plena velada. Y SIN --forzar: sin titulo no se
                        escribe nunca (ver ACTS_NEEDING_TITLE).
  --quitar-directo      id y titulo a NULL. Deja el evento "sin fijar": la web
                        vuelve a lo automatico.
  --ocultar-directo     id = 'off' y titulo a NULL. Es el INTERRUPTOR: la web lo
                        lee como "aqui no se pinta ningun directo", ni el fijado
                        ni el automatico, y apaga el reproductor sin desplegar.
                        Se deshace con --quitar-directo o con --directo.

SIN DESPLEGAR NO ES AL INSTANTE EN LA PORTADA. La ficha del evento y /en-vivo
leen la columna con 60 s de cache; la portada, de una consulta cacheada 30 min.
Por eso, tras escribir el directo, el script llama a /api/revalidate de la web
si tiene REVALIDATE_SECRET (en el entorno o en .env), y si no lo dice bien
claro y ensena el curl para hacerlo a mano.

Uso:
    # SIMULACION (lo que hace por defecto): ensena el antes y el despues
    .venv/Scripts/python.exe -m scripts.fijar_videos_evento --evento 1090 \
        --careo MQLCbgV5rhc --pesaje enkyfSnB0r0

    # Escribir de verdad
    .venv/Scripts/python.exe -m scripts.fijar_videos_evento --evento 1090 \
        --careo MQLCbgV5rhc --aplicar

    # Escribir el id aunque el oembed no conteste (el titulo quedara a NULL)
    .venv/Scripts/python.exe -m scripts.fijar_videos_evento --evento 1090 \
        --careo MQLCbgV5rhc --aplicar --forzar

    # El directo: fijarlo, quitarlo o apagarlo
    .venv/Scripts/python.exe -m scripts.fijar_videos_evento --evento 1088 \
        --directo "https://www.youtube.com/live/qM-h-OudTqM?si=abc" --aplicar
    .venv/Scripts/python.exe -m scripts.fijar_videos_evento --evento 1088 \
        --quitar-directo --aplicar
    .venv/Scripts/python.exe -m scripts.fijar_videos_evento --evento 1088 \
        --ocultar-directo --aplicar

Es idempotente: correrlo dos veces con los mismos ids no escribe la segunda vez.
Necesita las migraciones 027 (live_video_id, live_video_title) y 029
(faceoff_video_title, weighin_video_id, weighin_video_title) aplicadas; si
falta alguna, aborta diciendo cual en vez de reventar con un "column does not
exist".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

# Same guard the other operator scripts carry (event_readiness, live_watch,
# post_event_review, repair_incomplete_round_stats). It is NOT cosmetic here:
# every line this script prints is a YouTube title, and UFC titles routinely
# carry emoji and accents ("Rosas Jr and Font Face-off! [emoji] #ufc326"). On
# Windows stdout defaults to cp1252, so `print(info.title)` raised
# UnicodeEncodeError and the script died before showing the report -- reproduced
# on 18-sep-2026 at the "titulo ->" line. errors="replace" degrades to "?"
# instead of aborting: a mangled character is fine, losing the report is not.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Same threshold the web already uses to reject Shorts
# (mma-app/src/lib/youtube.ts: MIN_DURATION_SECONDS = 75). Kept in sync on
# purpose: the operator must see here what the site would reject there.
MIN_DURATION_SECONDS = 75

# A YouTube video id is exactly 11 chars of the URL-safe base64 alphabet.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# The oembed endpoint does NOT return the duration, so it comes from the /watch
# HTML, where the player config carries "lengthSeconds":"195".
_LENGTH_SECONDS_RE = re.compile(r'"lengthSeconds"\s*:\s*"(\d{1,6})"')

OEMBED_ENDPOINT = "https://www.youtube.com/oembed"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mma-ingesta/1.0)"}

# Columns of `events` this script reads and may write, by act. The names come
# from migrations 022 (faceoff_video_id), 027 (the live pair) and 029 (the
# other three).
ID_COLUMN = {
    "careo": "faceoff_video_id",
    "pesaje": "weighin_video_id",
    "directo": "live_video_id",
}
TITLE_COLUMN = {
    "careo": "faceoff_video_title",
    "pesaje": "weighin_video_title",
    "directo": "live_video_title",
}
ACTS = ("careo", "pesaje", "directo")

# Solo el directo acepta una URL ademas del id pelado: es el que se fija con
# prisa en plena velada, copiando de la barra del navegador o del boton
# Compartir. Careo y pesaje siguen pidiendo el id de 11 caracteres, como hasta
# ahora.
ACTS_ACCEPTING_URL = ("directo",)

# 🪤 Sin aviso de short para el directo. Mientras emite, o mientras esta
# programado, YouTube da "lengthSeconds":"0" en /watch, y 0 < 75: el aviso
# saltaria en TODOS los directos buenos, y un aviso que salta siempre es ruido
# que se aprende a ignorar. Cuando termina si da la duracion real (el directo
# de ejemplo de la migracion 027 da 3600 s, medido el 28-sep-2026), pero
# entonces ya no hace falta.
ACTS_WITHOUT_SHORT_WARNING = ("directo",)

# 🪤 Actos que NO se escriben sin titulo, ni con --forzar. La web no pinta un
# directo sin titulo (EventLiveEmbed no sabe decir que es), y como la columna
# manda, un id a mano sin titulo ademas apaga lo automatico: ni se detecta el
# directo ni sale UFC TV en la portada. Forzarlo era un --ocultar-directo
# disfrazado, que ademas decia "ok". El careo y el pesaje si se pintan sin
# titulo, y por eso alli --forzar sigue valiendo.
ACTS_NEEDING_TITLE = ("directo",)

# /api/revalidate de la web: invalida los tags 'home' y 'events' y el path '/'
# (mma-app/src/app/api/revalidate/route.ts), lo mismo que ya dispara
# refresh-news.yml. Sin el, la portada tarda hasta PORTADA_CACHE_MINUTOS en
# ver el directo nuevo: getNextEventHero se cachea 1800 s.
REVALIDATE_URL = "https://mmastatus.app/api/revalidate"
PORTADA_CACHE_MINUTOS = 30

# 🪤 EL INTERRUPTOR. Este literal en live_video_id NO es un video: la web lo lee
# como "en este evento no se pinta ningun directo", ni el fijado a mano ni el
# que encuentre lo automatico. Asi se apaga el reproductor de un evento SIN
# DESPLEGAR (en la portada, al momento solo si se revalida: ver
# refresh_home_page). La web compara con este mismo literal (LIVE_VIDEO_OFF en
# mma-app/src/lib/ufc-tv.ts; alli sin mayusculas ni espacios): si se cambia
# aqui, alli tambien. No choca con un id real: tiene 3 caracteres, un id 11.
LIVE_VIDEO_OFF = "off"

# Hosts from which extract_video_id takes an id. Anything else is refused:
# guessing an id out of a URL we do not know is how the wrong video gets in.
_YOUTUBE_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com")
_SHORT_LINK_HOSTS = ("youtu.be", "www.youtu.be")

# Everything the report prints, read in one go and by position: the fake cursor
# of the test suite has no `description`, and neither should this need one.
EVENT_COLUMNS = (
    "id",
    "name",
    "event_date",
    "status",
    "faceoff_video_id",
    "faceoff_video_title",
    "weighin_video_id",
    "weighin_video_title",
    "live_video_id",
    "live_video_title",
)

# Columns migrations 027 and 029 add. Checked before reading so a pending
# migration reports itself instead of blowing up as a Postgres UndefinedColumn.
COLUMNS_FROM_MIGRATION_027 = (
    "live_video_id",
    "live_video_title",
)
COLUMNS_FROM_MIGRATION_029 = (
    "faceoff_video_title",
    "weighin_video_id",
    "weighin_video_title",
)

# Each group with the file that adds it, so the abort names the migration to run.
REQUIRED_MIGRATIONS = (
    ("db/migrations/027_events_live_video.sql", COLUMNS_FROM_MIGRATION_027),
    ("db/migrations/029_events_weighin_video.sql", COLUMNS_FROM_MIGRATION_029),
)
REQUIRED_COLUMNS = tuple(
    column for _migration, columns in REQUIRED_MIGRATIONS for column in columns
)


@dataclass(frozen=True)
class VideoInfo:
    """What YouTube says about a video id, or why it could not be asked."""

    video_id: str
    title: str | None = None
    length_seconds: int | None = None
    error: str | None = None

    @property
    def resolved(self) -> bool:
        return self.title is not None

    @property
    def looks_like_a_short(self) -> bool:
        return (
            self.length_seconds is not None
            and self.length_seconds < MIN_DURATION_SECONDS
        )


@dataclass(frozen=True)
class LiveSwitch:
    """What --quitar-directo / --ocultar-directo leave in the live columns.

    It is not a video, so there is nothing to ask YouTube. It exposes the same
    two attributes as VideoInfo (video_id, title) so plan_changes treats both
    alike, and the title is ALWAYS None: neither NULL nor 'off' has a title,
    and keeping the old one would be the lying caption again.
    """

    video_id: str | None
    summary: str

    @property
    def title(self) -> None:
        return None


QUITAR_DIRECTO = LiveSwitch(
    video_id=None,
    summary="se quita el directo fijado (id y titulo a NULL): vuelve lo automatico",
)
OCULTAR_DIRECTO = LiveSwitch(
    video_id=LIVE_VIDEO_OFF,
    summary=(
        "INTERRUPTOR: id = %r y titulo a NULL. La web no pinta ningun directo "
        "en este evento, ni el fijado ni el automatico" % LIVE_VIDEO_OFF
    ),
)


@dataclass(frozen=True)
class Change:
    """One column of one event, with the value it had and the one it would get."""

    column: str
    before: object
    after: object


def build_parser() -> argparse.ArgumentParser:
    """CLI. Writing is OPT-IN: without --aplicar this only reports."""
    # Descripcion y epilogo van con los saltos de linea puestos a mano: el
    # formateador por defecto parte las palabras por el guion y dejaba
    # "--ocultar-" en una linea y "directo" en la siguiente.
    parser = argparse.ArgumentParser(
        prog="fijar_videos_evento",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Fija a mano el video del careo, del pesaje y/o del directo de un\n"
            "evento. Sin --aplicar solo ensena el antes y el despues."
        ),
        epilog=(
            "El directo tiene tres palancas, y solo se puede usar una a la vez:\n"
            "  --directo          lo fija a mano (manda sobre lo automatico)\n"
            "  --quitar-directo   lo deja sin fijar (vuelve lo automatico)\n"
            "  --ocultar-directo  lo APAGA con %r: la web no pinta nada,\n"
            "                     y sin desplegar\n"
            "Sin desplegar no es al instante en la portada: tras escribir el\n"
            "directo se llama a /api/revalidate si hay REVALIDATE_SECRET (entorno\n"
            "o .env); si no, la portada tarda hasta %d min (la ficha y /en-vivo,\n"
            "~1 min)." % (LIVE_VIDEO_OFF, PORTADA_CACHE_MINUTOS)
        ),
    )
    parser.add_argument("--evento", type=int, required=True, help="events.id")
    parser.add_argument("--careo", metavar="VIDEO_ID", help="id del careo")
    parser.add_argument("--pesaje", metavar="VIDEO_ID", help="id del pesaje")
    directo = parser.add_mutually_exclusive_group()
    directo.add_argument(
        "--directo",
        metavar="ID_O_URL",
        help=(
            "fija el directo a mano: escribe live_video_id y live_video_title "
            "(el titulo REAL, pedido al oembed). Vale el id o la URL: "
            "youtube.com/watch?v=<id>, youtu.be/<id> o youtube.com/live/<id>. "
            "Manda sobre lo automatico. No avisa de short: un directo en curso "
            "da 0 segundos"
        ),
    )
    directo.add_argument(
        "--quitar-directo",
        action="store_true",
        help=(
            "pone live_video_id y live_video_title a NULL a la vez. El evento "
            "queda sin directo fijado y la web vuelve a lo automatico"
        ),
    )
    directo.add_argument(
        "--ocultar-directo",
        action="store_true",
        help=(
            "INTERRUPTOR: escribe live_video_id=%r y live_video_title=NULL. La "
            "web no pinta ningun directo en ese evento (ni el fijado ni el "
            "automatico) y no hace falta desplegar. Se deshace con "
            "--quitar-directo (vuelve lo automatico) o con --directo (uno fijo)"
            % LIVE_VIDEO_OFF
        ),
    )
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="escribe de verdad (por defecto: simulacion)",
    )
    parser.add_argument(
        "--forzar",
        action="store_true",
        help=(
            "escribe el id aunque el oembed no conteste; el titulo quedara a "
            "NULL (nunca se deja el titulo viejo con un id nuevo). Solo careo "
            "y pesaje: un directo sin titulo la web no lo pinta, asi que no "
            "se escribe"
        ),
    )
    return parser


def is_valid_video_id(value: str | None) -> bool:
    """Shape check only: 11 chars of [A-Za-z0-9_-]. Says nothing about existence."""
    return bool(value) and bool(_VIDEO_ID_RE.match(value))


def extract_video_id(value: str | None) -> str | None:
    """The 11-char id out of what the operator pastes: a bare id or a URL.

    Accepted: the bare id, youtube.com/watch?v=<id>, youtu.be/<id> and
    youtube.com/live/<id> -- the three forms YouTube hands out for a live
    broadcast (address bar and the two "Share" links). Extra params (?si=,
    &t=, &ab_channel=) are ignored and the scheme is optional.

    Anything else is None, on purpose. A channel's live page
    (youtube.com/@ufcespanol/live) does not name ONE video: it points to
    whatever that channel is streaming now, which next Saturday is another one.
    """
    if not value:
        return None
    text = value.strip()
    if is_valid_video_id(text):
        return text
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:  # e.g. an unbalanced "[" in the host
        return None
    segments = [segment for segment in parts.path.split("/") if segment]

    candidate = None
    if host in _YOUTUBE_HOSTS:
        if segments == ["watch"]:
            candidate = (parse_qs(parts.query).get("v") or [None])[0]
        elif len(segments) == 2 and segments[0] == "live":
            candidate = segments[1]
    elif host in _SHORT_LINK_HOSTS and len(segments) == 1:
        candidate = segments[0]
    return candidate if is_valid_video_id(candidate) else None


def oembed_url(video_id: str) -> str:
    """Public oembed for a watch URL. No API key, no quota."""
    query = urlencode(
        {"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"}
    )
    return f"{OEMBED_ENDPOINT}?{query}"


def watch_url(video_id: str) -> str:
    return WATCH_URL.format(video_id=video_id)


def parse_oembed_title(body: str | None) -> str | None:
    """Title out of an oembed JSON body. None if it is missing or malformed."""
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    return title.strip()


def parse_length_seconds(html: str | None) -> int | None:
    """Duration in seconds out of the /watch HTML. None if it is not there.

    Missing duration is NOT an error: YouTube can serve a consent page or change
    its markup. It only means the Short warning cannot be given, and the report
    says so rather than pretending the video is long enough.
    """
    if not html:
        return None
    found = _LENGTH_SECONDS_RE.search(html)
    if not found:
        return None
    return int(found.group(1))


def fetch_text(url: str) -> str | None:
    """GET that returns the body, or None on any failure. THE ONLY NETWORK HERE.

    Injected in tests (`resolve_video(..., fetch=...)`) so the suite never opens
    a socket.
    """
    import requests  # inside the function: importing this module must not need it

    try:
        response = requests.get(url, headers=_HEADERS, timeout=20)
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    return response.text


def post_revalidate(secret: str) -> int | None:
    """POST to the web's /api/revalidate. HTTP status, or None if it never answered.

    The only OTHER network call here, and the only one that talks to the web.
    Replaced in tests (see the `ejecutar` fixture) so the suite never reaches
    production.
    """
    import requests  # inside the function: importing this module must not need it

    try:
        response = requests.post(
            REVALIDATE_URL,
            headers={**_HEADERS, "Authorization": f"Bearer {secret}"},
            timeout=20,
        )
    except requests.RequestException:
        return None
    return response.status_code


def revalidate_secret() -> str | None:
    """REVALIDATE_SECRET from the environment, else from .env. None if absent.

    It is the same secret refresh-news.yml uses. It is optional on purpose: the
    DB write is the job, refreshing the home page only shortens the wait.
    """
    valor = os.environ.get("REVALIDATE_SECRET", "").strip()
    if valor:
        return valor
    ruta = os.path.join(RAIZ, ".env")
    if not os.path.exists(ruta):
        return None
    for linea in open(ruta, encoding="utf-8"):
        if linea.startswith("REVALIDATE_SECRET="):
            return linea.split("=", 1)[1].strip().strip('"') or None
    return None


def refresh_home_page() -> None:
    """After writing the live columns: make the home page see it now, or say when.

    The event page and /en-vivo read the column with a 60 s cache, the home page
    through getNextEventHero, cached 30 min. Without this, --ocultar-directo in
    the middle of a fight night left the wrong video on the home page for half
    an hour while the script said everything was done.
    """
    print()
    print("PORTADA")
    secreto = revalidate_secret()
    if not secreto:
        print(
            "  AVISO: sin REVALIDATE_SECRET (entorno o .env) no se puede refrescar."
        )
        print(
            "  La ficha del evento y /en-vivo lo ven en ~1 min; la PORTADA puede "
            "tardar hasta %d min." % PORTADA_CACHE_MINUTOS
        )
        print("  Para que sea ya (el secreto es el de refresh-news.yml):")
        print(
            '    curl -X POST %s -H "Authorization: Bearer $REVALIDATE_SECRET"'
            % REVALIDATE_URL
        )
        return
    codigo = post_revalidate(secreto)
    if codigo == 200:
        print("  revalidada (HTTP 200): la proxima visita ya lo ensena.")
        return
    motivo = "sin respuesta" if codigo is None else "HTTP %s" % codigo
    print(
        "  AVISO: /api/revalidate no la refresco (%s). La portada puede tardar "
        "hasta %d min; la ficha y /en-vivo, ~1 min." % (motivo, PORTADA_CACHE_MINUTOS)
    )


def resolve_video(video_id: str, fetch=None) -> VideoInfo:
    """Ask YouTube for the real title and the duration of a video id.

    The title is the load-bearing half: without it the write is blocked (unless
    --forzar), because an id whose video does not exist leaves a dead player.

    `fetch` defaults to None and is resolved HERE and not in the signature so
    that patching the module's `fetch_text` reaches this call too.
    """
    fetch = fetch or fetch_text
    if not is_valid_video_id(video_id):
        return VideoInfo(
            video_id=video_id,
            error="no tiene forma de id de YouTube (11 caracteres [A-Za-z0-9_-])",
        )

    title = parse_oembed_title(fetch(oembed_url(video_id)))
    if title is None:
        return VideoInfo(
            video_id=video_id,
            error="el oembed no contesto: el video no existe, es privado o YouTube fallo",
        )

    length_seconds = parse_length_seconds(fetch(watch_url(video_id)))
    return VideoInfo(video_id=video_id, title=title, length_seconds=length_seconds)


def plan_changes(
    current: dict, requested: dict[str, VideoInfo | LiveSwitch]
) -> list[Change]:
    """Columns that would actually change. Empty list = nothing to do.

    The id and its title travel TOGETHER: writing an id always writes its title
    column, with the resolved title or with None. Leaving the previous title next
    to a new id is the lie migration 029 was written to prevent. A LiveSwitch
    follows the same rule: NULL or 'off' in the id, and NULL in the title.
    """
    changes: list[Change] = []
    for act, info in requested.items():
        id_column = ID_COLUMN[act]
        title_column = TITLE_COLUMN[act]
        for column, after in ((id_column, info.video_id), (title_column, info.title)):
            before = current.get(column)
            if before != after:
                changes.append(Change(column=column, before=before, after=after))
    return changes


def build_update(event_id: int, changes: list[Change]) -> tuple[str, tuple]:
    """UPDATE that OVERWRITES. That is the whole point of this script.

    No `AND ... IS NULL` guard here, unlike set_event_faceoff_video: the cron
    must never overwrite, and the operator must always be able to.
    `events` has no updated_at column (see db/migrations), so nothing else is set.
    """
    assignments = ", ".join(f"{change.column} = %s" for change in changes)
    params = tuple(change.after for change in changes) + (event_id,)
    return f"UPDATE events SET {assignments} WHERE id = %s", params


def missing_columns(cursor, columns: tuple[str, ...]) -> list[str]:
    """Which of those columns `events` does not have yet (migration pending)."""
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'events' AND column_name = ANY(%s)
        """,
        (list(columns),),
    )
    present = {row[0] for row in cursor.fetchall()}
    return [column for column in columns if column not in present]


def read_event(cursor, event_id: int) -> dict | None:
    """The event row as a dict, read by position (no cursor.description)."""
    cursor.execute(
        f"SELECT {', '.join(EVENT_COLUMNS)} FROM events WHERE id = %s", (event_id,)
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip(EVENT_COLUMNS, row))


def _dsn() -> str:
    ruta = os.path.join(RAIZ, ".env")
    for linea in open(ruta, encoding="utf-8"):
        if linea.startswith("DATABASE_URL="):
            return linea.split("=", 1)[1].strip().strip('"')
    raise SystemExit("No hay DATABASE_URL en .env")


def _connect(dsn: str):
    import psycopg2  # inside: importing this module must not require the DB

    return psycopg2.connect(dsn)


def _muestra(valor) -> str:
    return "(vacio)" if valor is None else str(valor)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)

    peticion_cruda = {act: getattr(args, act) for act in ACTS if getattr(args, act)}
    # Quitar y ocultar no llevan video: fijan el valor de las dos columnas del
    # directo. El parser ya impide juntarlos entre si o con --directo.
    interruptor = (
        QUITAR_DIRECTO
        if args.quitar_directo
        else OCULTAR_DIRECTO if args.ocultar_directo else None
    )
    if not peticion_cruda and interruptor is None:
        print(
            "ABORTA: hay que dar al menos --careo, --pesaje, --directo, "
            "--quitar-directo u --ocultar-directo."
        )
        return 2

    # 1) Forma del id, antes de gastar una peticion o abrir la base. Al directo
    #    se le saca antes el id de la URL, si lo que llega es una URL.
    peticion: dict[str, str] = {}
    malos: list[tuple[str, str]] = []
    for act, crudo in peticion_cruda.items():
        video_id = extract_video_id(crudo) if act in ACTS_ACCEPTING_URL else crudo
        if is_valid_video_id(video_id):
            peticion[act] = video_id
        else:
            malos.append((act, crudo))
    if malos:
        for act, crudo in malos:
            if act not in ACTS_ACCEPTING_URL:
                print(
                    "ABORTA: %r no tiene forma de id de YouTube (--%s). "
                    "Son 11 caracteres de [A-Za-z0-9_-]." % (crudo, act)
                )
                continue
            print(
                "ABORTA: %r no es un id de YouTube ni una URL de las que se "
                "aceptan (--%s). Valen el id de 11 caracteres, "
                "youtube.com/watch?v=<id>, youtu.be/<id> y youtube.com/live/<id>."
                % (crudo, act)
            )
            if crudo.strip().lower() == LIVE_VIDEO_OFF:
                print("Para APAGAR el directo de este evento: --ocultar-directo.")
        return 1

    # 2) Que el video EXISTA, y como se llama de verdad.
    print("VIDEOS")
    requested: dict[str, VideoInfo | LiveSwitch] = {}
    sin_resolver: list[str] = []
    for act, video_id in peticion.items():
        info = resolve_video(video_id)
        requested[act] = info
        crudo = peticion_cruda[act]
        if crudo.strip() == video_id:
            print("  %-7s %s" % (act, video_id))
        else:
            print("  %-7s %s   (sacado de %s)" % (act, video_id, crudo))
        if info.resolved:
            print("          titulo -> %s" % info.title)
        elif act in ACTS_NEEDING_TITLE:
            print("          AVISO: %s" % info.error)
            print("          sin titulo NO se escribe, ni con --forzar: la web no")
            print("          pinta un directo sin titulo (ver ACTS_NEEDING_TITLE).")
            sin_resolver.append(act)
        else:
            print("          AVISO: %s" % info.error)
            print("          el titulo NO se escribira (quedaria a NULL).")
            sin_resolver.append(act)
        if info.length_seconds is None:
            print("          duracion -> no se pudo leer del HTML de /watch")
        else:
            print("          duracion -> %s s" % info.length_seconds)
        if info.looks_like_a_short and act in ACTS_WITHOUT_SHORT_WARNING:
            # Ver ACTS_WITHOUT_SHORT_WARNING: aqui la duracion no dice nada.
            print(
                "          (normal en un directo: mientras emite o esta "
                "programado YouTube da 0 s)"
            )
        elif info.looks_like_a_short:
            print("          " + "!" * 62)
            print(
                "          AVISO GORDO: esto parece un SHORT de %s segundos "
                "(< %s)." % (info.length_seconds, MIN_DURATION_SECONDS)
            )
            print(
                "          Es EXACTAMENTE el fallo del evento 1090: un short de "
                "17 s"
            )
            print("          publicado como careo oficial. Comprueba el video.")
            print("          " + "!" * 62)
    if interruptor is not None:
        requested["directo"] = interruptor
        print("  %-7s %s" % ("directo", interruptor.summary))

    conn = _connect(_dsn())
    cur = conn.cursor()

    # Todas las columnas que lee read_event, y no solo las del acto pedido:
    # la lectura es una sola SELECT con todas.
    faltan = missing_columns(cur, REQUIRED_COLUMNS)
    if faltan:
        print()
        print(
            "ABORTA: a la tabla events le faltan estas columnas: %s"
            % ", ".join(faltan)
        )
        for migracion, columnas in REQUIRED_MIGRATIONS:
            if any(columna in faltan for columna in columnas):
                print("Aplica %s y repite." % migracion)
        conn.close()
        return 1

    current = read_event(cur, args.evento)
    if current is None:
        print()
        print("ABORTA: no existe events.id=%s" % args.evento)
        conn.close()
        return 1

    print()
    print(
        "EVENTO %s  %s  (%s, %s)"
        % (
            current["id"],
            current["name"],
            _muestra(current["event_date"]),
            current["status"],
        )
    )

    changes = plan_changes(current, requested)

    print()
    print("CAMBIOS")
    for act in requested:
        for column in (ID_COLUMN[act], TITLE_COLUMN[act]):
            despues = next(
                (c.after for c in changes if c.column == column), current.get(column)
            )
            marca = "*" if any(c.column == column for c in changes) else " "
            print("%s %s" % (marca, column))
            print("      antes   -> %s" % _muestra(current.get(column)))
            print("      despues -> %s" % _muestra(despues))

    if not changes:
        print()
        print("Nada que cambiar: la base ya dice eso. (Es idempotente.)")
        conn.close()
        return 0

    if not args.aplicar:
        print()
        print("SIMULACION. Nada escrito. Repite con --aplicar para ejecutarlo.")
        conn.close()
        return 0

    sin_titulo_obligado = [act for act in sin_resolver if act in ACTS_NEEDING_TITLE]
    if sin_titulo_obligado:
        print()
        print(
            "ABORTA: no se pudo leer el titulo de: %s. Aqui --forzar no vale."
            % ", ".join(sin_titulo_obligado)
        )
        print(
            "La web NO pinta un directo sin titulo, y como la columna manda, "
            "un id sin"
        )
        print(
            "titulo ademas apaga lo automatico: ni se detecta el directo ni "
            "sale UFC TV"
        )
        print(
            "en la portada. Seria un --ocultar-directo disfrazado. Comprueba "
            "el id y"
        )
        print(
            "repite cuando el oembed conteste. Para apagarlo de verdad: "
            "--ocultar-directo."
        )
        conn.close()
        return 1

    if sin_resolver and not args.forzar:
        print()
        print(
            "ABORTA: no se pudo confirmar que exista el video de: %s."
            % ", ".join(sin_resolver)
        )
        print(
            "Un id que no resuelve deja un reproductor muerto en la web. "
            "Comprueba el id."
        )
        print(
            "Si sabes que el video es bueno, el id SI se puede escribir con "
            "--forzar (el titulo quedara a NULL)."
        )
        conn.close()
        return 1

    sql, params = build_update(args.evento, changes)
    cur.execute(sql, params)
    # 🪤 Commit EXPLICITO. psycopg2 abre transaccion y no hace autocommit, y
    # connect() de src/scrapers/db.py tampoco hace commit (incluso hace
    # rollback al devolver al pool): sin esta linea el UPDATE se pierde callado.
    conn.commit()

    print()
    print("COMPROBACION EN CALIENTE")
    despues = read_event(cur, args.evento)
    for change in changes:
        print(
            "  %-22s %s" % (change.column, _muestra((despues or {}).get(change.column)))
        )
    conn.close()

    # Solo el directo sale en la portada (careo y pesaje viven en la ficha y en
    # /en-vivo, con 60 s de cache), y revalidar tiene su coste: tambien vacia
    # las caches de UFC TV que se leen desde la portada. Por eso no se hace
    # para los otros.
    if any(change.column in COLUMNS_FROM_MIGRATION_027 for change in changes):
        refresh_home_page()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
