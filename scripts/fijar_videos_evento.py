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

Es idempotente: correrlo dos veces con los mismos ids no escribe la segunda vez.
Necesita la migracion 029 aplicada (faceoff_video_title, weighin_video_id,
weighin_video_title); si falta, aborta diciendolo en vez de reventar con un
"column does not exist".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlencode

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
# from migrations 022 (faceoff_video_id) and 029 (the other three).
ID_COLUMN = {"careo": "faceoff_video_id", "pesaje": "weighin_video_id"}
TITLE_COLUMN = {"careo": "faceoff_video_title", "pesaje": "weighin_video_title"}
ACTS = ("careo", "pesaje")

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
)

# Columns migration 029 adds. Checked before reading so a pending migration
# reports itself instead of blowing up as a Postgres UndefinedColumn.
COLUMNS_FROM_MIGRATION_029 = (
    "faceoff_video_title",
    "weighin_video_id",
    "weighin_video_title",
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
class Change:
    """One column of one event, with the value it had and the one it would get."""

    column: str
    before: object
    after: object


def build_parser() -> argparse.ArgumentParser:
    """CLI. Writing is OPT-IN: without --aplicar this only reports."""
    parser = argparse.ArgumentParser(
        prog="fijar_videos_evento",
        description=(
            "Fija a mano el video del careo y/o del pesaje de un evento. "
            "Sin --aplicar solo ensena el antes y el despues."
        ),
    )
    parser.add_argument("--evento", type=int, required=True, help="events.id")
    parser.add_argument("--careo", metavar="VIDEO_ID", help="id del careo")
    parser.add_argument("--pesaje", metavar="VIDEO_ID", help="id del pesaje")
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
            "NULL (nunca se deja el titulo viejo con un id nuevo)"
        ),
    )
    return parser


def is_valid_video_id(value: str | None) -> bool:
    """Shape check only: 11 chars of [A-Za-z0-9_-]. Says nothing about existence."""
    return bool(value) and bool(_VIDEO_ID_RE.match(value))


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


def plan_changes(current: dict, requested: dict[str, VideoInfo]) -> list[Change]:
    """Columns that would actually change. Empty list = nothing to do.

    The id and its title travel TOGETHER: writing an id always writes its title
    column, with the resolved title or with None. Leaving the previous title next
    to a new id is the lie migration 029 was written to prevent.
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
    if not peticion_cruda:
        print("ABORTA: hay que dar al menos --careo o --pesaje.")
        return 2

    # 1) Forma del id, antes de gastar una peticion o abrir la base.
    malos = [
        (act, video_id)
        for act, video_id in peticion_cruda.items()
        if not is_valid_video_id(video_id)
    ]
    if malos:
        for act, video_id in malos:
            print(
                "ABORTA: %r no tiene forma de id de YouTube (--%s). "
                "Son 11 caracteres de [A-Za-z0-9_-]." % (video_id, act)
            )
        return 1

    # 2) Que el video EXISTA, y como se llama de verdad.
    print("VIDEOS")
    requested: dict[str, VideoInfo] = {}
    sin_resolver: list[str] = []
    for act, video_id in peticion_cruda.items():
        info = resolve_video(video_id)
        requested[act] = info
        print("  %-7s %s" % (act, video_id))
        if info.resolved:
            print("          titulo -> %s" % info.title)
        else:
            print("          AVISO: %s" % info.error)
            print("          el titulo NO se escribira (quedaria a NULL).")
            sin_resolver.append(act)
        if info.length_seconds is None:
            print("          duracion -> no se pudo leer del HTML de /watch")
        else:
            print("          duracion -> %s s" % info.length_seconds)
        if info.looks_like_a_short:
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

    conn = _connect(_dsn())
    cur = conn.cursor()

    faltan = missing_columns(cur, COLUMNS_FROM_MIGRATION_029)
    if faltan:
        print()
        print(
            "ABORTA: a la tabla events le faltan estas columnas: %s"
            % ", ".join(faltan)
        )
        print("Aplica db/migrations/029_events_weighin_video.sql y repite.")
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
    for act in peticion_cruda:
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
    conn.commit()

    print()
    print("COMPROBACION EN CALIENTE")
    despues = read_event(cur, args.evento)
    for change in changes:
        print(
            "  %-22s %s" % (change.column, _muestra((despues or {}).get(change.column)))
        )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
