"""Match each upcoming event to its official UFC "Fighter Face-offs" (careo)
video and store the YouTube id (migration 022).

UFC publishes the face-off the evening before an event as
"UFC <City>: Fighter Face-offs" (Fight Nights) or "UFC <N>: ... Face-offs"
(numbered PPVs) on its official channel. We read the channel RSS feed (no API
key needed) and match a video to an event; the event page embeds it next to the
poster.

RESCUE PASS (opt-in): the RSS feed only carries the ~15 latest uploads, so a
careo drops out of it once an event passes and newer videos pile on. When a
YOUTUBE_API_KEY is present, a second pass pages the channel's uploads playlist
(playlistItems, 1 quota unit/page) back over the last ``--rescue-days`` and
rescues face-offs for recently-passed events the RSS could no longer reach.
Same conservative guard and first-writer-wins write; without a key the cron
runs RSS-only, exactly as before.

MATCH GUARD (conservative, mirrors backfill_fight_videos.is_trusted_match) — a
video is accepted only when ALL hold on the accent-stripped, casefolded title:
  1. matches face-?offs?  (whitelist; excludes "Ceremonial Weigh-In", "Weigh-Ins")
  2. contains a distinctive token of the event — a city token of its `location`
     (or the 'vegas' alias for Nevada/Apex cards), a BRAND token of its `name`
     ('noche' for the Noche UFC cards) — OR the "ufc <N>" card number, WITH a
     real separator: a glued "#ufc331" hashtag does NOT count (see
     _TITLE_NUM_TEMPLATE; that gap cost the 1090 its careo)
  3. published within [event_date - 2d, event_date + 1d] (measured, see _DAYS_BEFORE)
  4. lasts at least MIN_DURATION_SECONDS, when a YOUTUBE_API_KEY lets us ask —
     the guard against promo shorts. Without a key it degrades to OPEN, on
     purpose: see _duration_ok.
Better to miss than to mis-attribute: an unmatched event stays NULL and retries
on the next daily run. Writes are first-writer-wins (set_event_faceoff_video).

Usage:
    python -m src.scrapers.match_faceoffs --dump-feed   # print the channel feed, no DB
    python -m src.scrapers.match_faceoffs               # dry-run: report matches, no writes
    python -m src.scrapers.match_faceoffs --apply       # persist matches
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

import requests

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .matching import strip_accents
from .repositories.events import set_event_faceoff_video

LOGGER = logging.getLogger(__name__)

# Official UFC channel. RSS gives the latest ~15 uploads (id + title + date) with
# no API key. The face-off publishes the day before, so it's always in-window.
UFC_CHANNEL_ID = "UCvgfXK4nTYKudb0rFR6noLA"
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mma-ingesta/1.0)"}

_FACEOFF_RE = re.compile(r"face-?offs?\b", re.IGNORECASE)
_UFC_NUM_RE = re.compile(r"\bufc\s*(\d{2,4})\b", re.IGNORECASE)

# 🪤 LA GRIETA DEL HASHTAG. El careo del 1090 se lo llevo un SHORT de 17 s
# titulado "what are these faceoffs saying?! #ufc331".
#
# Hasta hoy el numero de cartelera se buscaba en el titulo del video con el
# MISMO patron que en el nombre del evento: `\bufc\s*331\b`. Y ese `\s*` admite
# CERO espacios, asi que "#ufc331" casaba: la almohadilla es frontera de
# palabra, de modo que un hashtag de short era literalmente indistinguible del
# formato oficial "UFC 331: ...". Con eso el short pasaba la guarda 2, y la
# escritura es first-writer-wins: el careo bueno ya no podia entrar nunca.
#
# Aqui el patron del TITULO se separa del patron del NOMBRE y exige DOS cosas:
#   - `\s+`  — un separador de verdad, no cero,
#   - `(?<!#)` — y que "ufc" no venga pegado a una almohadilla.
# Medido el 18-sep-2026 sobre las 20.000 subidas del canal oficial
# UCvgfXK4nTYKudb0rFR6noLA, de las que 477 llevan "face-off" en el titulo:
#   - 14 escriben "ufc<num>" PEGADO, y las 14 son shorts/clips promocionales
#     ("Rosas Jr and Font Face-off! 💥 #ufc326"). NI UNA es un careo de verdad.
#   - 0 careos usan la forma pegada sin almohadilla, y 0 usan "#ufc <num>".
#   - los 30 careos que hoy estan en la base usan "UFC <N>: ..." con espacio.
# O sea: el patron pegado es EXCLUSIVO de los hashtags de short. Cerrarlo no
# cuesta ni un solo positivo legitimo.
#
# El patron del nombre de evento (_UFC_NUM_RE) NO se toca: ese texto lo produce
# nuestro propio scraper, y de los 798 nombres reales 0 llevan la forma pegada.
#
# ⚠️ Y LO MEDIDO VALE SOLO PARA ESTE CANAL, EL DE LA UFC EN INGLES. El canal
# OFICIAL EN ESPANOL (ufcespanol) SI titula careos de verdad con la almohadilla
# pegada, y el contraejemplo es justo el video de este incidente:
#   MQLCbgV5rhc · "#CryptoCom #UFC331: Careos Conferencia de Prensa" · 3:15
# o sea, el careo BUENO del 1090. Hoy no pasa nada, porque este modulo solo lee
# UFC_CHANNEL_ID y ademas "Careos" no pasa _FACEOFF_RE. Pero quien anada el
# canal en espanol y el termino "careo" a la lista blanca TIENE que aflojar esta
# regla para esos titulos (o exigir el separador solo en el canal ingles): si no,
# el careo oficial en espanol se rechazaria por la defensa que se puso para
# protegerlo. Verificado el 18-sep-2026 con el oembed de YouTube.
_TITLE_NUM_TEMPLATE = r"(?<!#)\bufc\s+{num}\b"

# Careos publish the evening before; allow a small window around the event date.
#
# 🪤 LA VENTANA ERA DE 5 DIAS Y NADIE LA HABIA MEDIDO. El short del 1090 se
# publico a 3 dias del evento y entro por aqui. Medido el 18-sep-2026 cruzando
# `events.event_date` con el `publishedAt` real (YouTube Data API) de los 30
# careos que hay en la base:
#     delta 0 dias -> 8 videos      delta 1 dia -> 22 videos
#     delta 2 dias -> 0             delta 3+ dias -> 0
# El desfase MAXIMO real es de UN dia, no de dos ni de tres. Con 2 queda un dia
# entero de margen sobre el peor caso medido y el short de 3 dias se queda
# fuera el solo, sin necesidad de API ni de cuota.
_DAYS_BEFORE = 2
_DAYS_AFTER = 1

# Minimum video length for a careo, in seconds. MISMO UMBRAL QUE LA WEB: ver
# mma-app/src/lib/youtube.ts:126 (MIN_DURATION_SECONDS = 75), que ya lo usa para
# descartar shorts al pintar el embed. Los 30 careos reales de la base duran
# entre 152 s y 1363 s, asi que 75 s deja un margen de 2x sobre el mas corto
# legitimo y corta en seco los shorts verticales (el del incidente: 17 s).
MIN_DURATION_SECONDS = 75

# --- Optional YouTube Data API rescue (opt-in via YOUTUBE_API_KEY) --------
# The RSS feed only exposes the ~15 latest uploads, so a careo drops out of it
# once newer videos pile on (e.g. an event that just passed). playlistItems on
# the channel's uploads playlist pages further back at 1 quota unit/page (vs 100
# for search.list), letting the daily cron RESCUE those missed face-offs.
PLAYLIST_ITEMS_ENDPOINT = "https://www.googleapis.com/youtube/v3/playlistItems"
# videos.list?part=contentDetails is the ONLY place a duration comes from: neither
# the Atom feed nor playlistItems carries it. 1 quota unit per batch of up to 50
# ids, and we only ever ask about titles that already passed the face-off
# whitelist (0-3 per run), so the duration guard costs 1 unit/day.
VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"
_VIDEOS_BATCH = 50
_RESCUE_LOOKBACK_DAYS = 45
# Safety cap on pagination. The published_after early-stop normally trips first;
# 20 pages (~1000 videos, 20 quota units of 10k/day) is enough to reach ~45 days
# back even on the very active UFC channel.
_UPLOADS_MAX_PAGES = 20

# A fetcher receives the request params and returns the parsed JSON body
# (injected in tests so the rescue path never touches the network).
_PlaylistFetcher = Callable[[dict], dict]


@dataclass(frozen=True)
class FeedVideo:
    video_id: str
    title: str
    published: date


@dataclass(frozen=True)
class TargetEvent:
    id: int
    name: str
    location: str | None
    event_date: date | None


def parse_feed(xml_text: str) -> list[FeedVideo]:
    """Parse a YouTube channel Atom feed into (videoId, title, published date)."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        LOGGER.warning("Feed parse error: %s", exc)
        return []
    videos: list[FeedVideo] = []
    for entry in root.findall(f"{_ATOM}entry"):
        vid = entry.findtext(f"{_YT}videoId")
        title = entry.findtext(f"{_ATOM}title")
        published = entry.findtext(f"{_ATOM}published")
        if not vid or not title or not published:
            continue
        try:
            pub_date = datetime.fromisoformat(published).date()
        except ValueError:
            continue
        videos.append(FeedVideo(video_id=vid.strip(), title=title.strip(), published=pub_date))
    return videos


def fetch_channel_feed(
    session: requests.Session, channel_id: str = UFC_CHANNEL_ID
) -> list[FeedVideo]:
    """Latest uploads of a YouTube channel via its public RSS (no API key)."""
    try:
        response = session.get(RSS_URL.format(channel_id=channel_id), headers=_HEADERS, timeout=20)
    except requests.RequestException as exc:
        LOGGER.warning("Feed fetch failed: %s", exc)
        return []
    if not response.ok:
        LOGGER.warning("Feed HTTP %s", response.status_code)
        return []
    return parse_feed(response.text)


def uploads_playlist_id(channel_id: str = UFC_CHANNEL_ID) -> str:
    """A channel's uploads playlist id: the channel id with its 'UC' prefix
    swapped for 'UU' (a stable YouTube convention). Lets us page every upload via
    playlistItems (1 quota unit/page) without an extra channels.list call."""
    return "UU" + channel_id[2:] if channel_id.startswith("UC") else channel_id


def parse_playlist_page(data: dict) -> list[FeedVideo]:
    """Parse one playlistItems API page into FeedVideo (id, title, published).

    Prefers contentDetails.videoPublishedAt (the real upload time) and falls back
    to the snippet fields; skips items missing any of id/title/date so a partial
    entry never becomes a bogus match."""
    videos: list[FeedVideo] = []
    for item in data.get("items", []):
        content = item.get("contentDetails") or {}
        snippet = item.get("snippet") or {}
        vid = content.get("videoId")
        if not vid:
            vid = (snippet.get("resourceId") or {}).get("videoId")
        title = snippet.get("title")
        raw = content.get("videoPublishedAt") or snippet.get("publishedAt")
        if not vid or not title or not raw:
            continue
        try:
            pub = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        videos.append(FeedVideo(vid.strip(), title.strip(), pub))
    return videos


def _requests_playlist_fetcher(timeout: int = 15) -> _PlaylistFetcher:
    def fetch(params: dict) -> dict:
        response = requests.get(PLAYLIST_ITEMS_ENDPOINT, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

    return fetch


def fetch_channel_uploads(
    api_key: str,
    *,
    published_after: date | None = None,
    channel_id: str = UFC_CHANNEL_ID,
    max_pages: int = _UPLOADS_MAX_PAGES,
    fetcher: _PlaylistFetcher | None = None,
) -> list[FeedVideo]:
    """Recent uploads of a channel via playlistItems (needs a quota key), newest
    first, paging back until an item predates ``published_after`` or ``max_pages``
    is hit. Returns FeedVideo objects so match_event reuses the exact RSS guard."""
    fetch = fetcher or _requests_playlist_fetcher()
    playlist_id = uploads_playlist_id(channel_id)
    videos: list[FeedVideo] = []
    page_token: str | None = None
    reached_old = False
    oldest: date | None = None
    for _ in range(max_pages):
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        data = fetch(params)
        reached_old = False
        for video in parse_playlist_page(data):
            if oldest is None or video.published < oldest:
                oldest = video.published
            if published_after is not None and video.published < published_after:
                reached_old = True
                continue
            videos.append(video)
        page_token = data.get("nextPageToken")
        if reached_old or not page_token:
            break

    # 🪤 EL TOPE SE AGOTÓ SIN LLEGAR A LA FECHA PEDIDA, Y HASTA HOY ESO ERA MUDO.
    #
    # El bucle salía del `for` sin log ni excepción, así que quien lo llamaba
    # recibía una lista corta indistinguible de «el canal no tiene más vídeos».
    # El rescate seguía adelante y devolvía «no match» para eventos cuyos vídeos
    # NI SIQUIERA SE DESCARGARON — que es exactamente la mentira que este
    # proyecto persigue: no es que mirase y no estuviera, es que no miró.
    #
    # El aviso lleva la fecha REALMENTE alcanzada dentro. Un «truncado» a secas
    # no dice cuánto falta, y sin eso nadie sabe qué `--max-pages` pedir.
    if published_after is not None and not reached_old and page_token:
        LOGGER.warning(
            "Uploads TRUNCADO: %d paginas agotadas y solo se llego a %s, "
            "no a %s. Faltan videos por mirar; sube --max-pages.",
            max_pages,
            oldest,
            published_after,
        )
    return videos


# --- Duration guard (the net under the title guards) ----------------------

_ISO8601_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$",
    re.IGNORECASE,
)


def parse_iso8601_duration(text: str | None) -> int | None:
    """Seconds from a YouTube ISO-8601 duration ("PT3M15S" -> 195).

    Returns None — NOT 0 — for anything unparseable, so an unknown duration can
    never be mistaken for a zero-length video and get a legitimate careo
    rejected. A live stream still in progress reports "P0D", which parses to 0
    seconds and is correctly treated as too short."""
    if not text:
        return None
    match = _ISO8601_DURATION_RE.match(text.strip())
    if not match:
        return None
    raw = match.groupdict()
    # 🪤 TODOS LOS COMPONENTES SON OPCIONALES EN EL PATRON, asi que "P" y "PT" a
    # secas CASABAN y salian valiendo 0 segundos — o sea "demasiado corto", que
    # es justo lo que este parser no puede decir cuando no ha entendido nada. Lo
    # cazo test_parse_iso8601_devuelve_none_y_no_cero_cuando_no_entiende. Sin un
    # solo componente presente no hay duracion, hay basura: None.
    if all(v is None for v in raw.values()):
        return None
    parts = {k: int(v) if v else 0 for k, v in raw.items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _requests_videos_fetcher(timeout: int = 15) -> _PlaylistFetcher:
    def fetch(params: dict) -> dict:
        response = requests.get(VIDEOS_ENDPOINT, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

    return fetch


def fetch_video_durations(
    api_key: str,
    video_ids: list[str],
    *,
    fetcher: _PlaylistFetcher | None = None,
) -> dict[str, int]:
    """Durations in seconds for the given video ids, via videos.list.

    Returns ONLY the ids it could actually resolve. An id missing from the
    result means "we don't know", never "it's short" — see _duration_ok for why
    that distinction is the whole design of the degraded path.

    Never raises: a network error, a revoked key or a quota wall logs a WARNING
    and yields an empty dict, which degrades the guard to open rather than
    silently rejecting every careo."""
    if not api_key or not video_ids:
        return {}
    fetch = fetcher or _requests_videos_fetcher()
    durations: dict[str, int] = {}
    unique_ids = list(dict.fromkeys(video_ids))
    for start in range(0, len(unique_ids), _VIDEOS_BATCH):
        batch = unique_ids[start : start + _VIDEOS_BATCH]
        try:
            data = fetch(
                {
                    "part": "contentDetails",
                    "id": ",".join(batch),
                    "key": api_key,
                }
            )
        except Exception as exc:  # noqa: BLE001 - the guard degrades, it never breaks the run
            LOGGER.warning(
                "Duracion: videos.list fallo (%s) para %d ids; "
                "esos videos quedan SIN comprobar (guarda degradada a abierta).",
                exc,
                len(batch),
            )
            continue
        for item in data.get("items", []):
            video_id = item.get("id")
            seconds = parse_iso8601_duration(
                (item.get("contentDetails") or {}).get("duration")
            )
            if video_id and seconds is not None:
                durations[video_id] = seconds
    return durations


def _duration_ok(video_id: str, durations: dict[str, int] | None) -> bool:
    """Whether a candidate clears the length guard.

    ⚠️ ESTA FUNCION ES DELIBERADAMENTE PERMISIVA, Y ESA ES LA DECISION DE DISENO
    MAS IMPORTANTE DEL FILTRO. Solo devuelve False cuando SABEMOS la duracion y
    es corta. Sin `durations` (no hay YOUTUBE_API_KEY) o con el id ausente (la
    llamada fallo, o YouTube no devolvio esa fila) devuelve True.

    El razonamiento, porque la alternativa es tentadora y es PEOR que el bug:
    rechazar por defecto dejaria TODOS los eventos sin careo en cuanto faltase
    el secreto o se agotase la cuota — una averia total y MUDA de la funcion,
    justo lo que este proyecto persigue. El bug que arreglamos costo UN careo
    mal; el fail-closed costaria TODOS, y ademas se dispararia por la causa mas
    tonta (un secreto sin renovar en GitHub Actions).

    Y puede ser permisiva porque ESTA GUARDA ES LA RED, NO LA UNICA DEFENSA. Las
    otras dos del incidente funcionan sin API y sin cuota: el short "#ufc331" ya
    no pasa el patron del numero (grieta del hashtag) y se publico a 3 dias, o
    sea fuera de la ventana de 2. Cualquiera de las dos lo habria bloqueado
    sola. La duracion cubre el caso futuro que aun no hemos visto.

    El fallo deja rastro: fetch_video_durations avisa por WARNING, asi que la
    guarda degradada es visible en el log, no silenciosa."""
    if durations is None:
        return True
    seconds = durations.get(video_id)
    if seconds is None:
        return True
    return seconds >= MIN_DURATION_SECONDS


def _norm(text: str | None) -> str:
    return strip_accents(text or "").casefold()


# Words that do NOT tell one card apart from another, so they cannot act as a
# guard. Three groups, all chosen from the 185 real `events.location` rows:
#  - generic articles/fillers that survive the 3-char cut,
#  - VENUE TYPES: they name a building, not a place. "Belgrade Arena" is
#    Belgrade; "arena" on its own would match any other card's faceoff video,
#  - country/state words too common to discriminate: `usa` appears in 91 of the
#    185 rows, so it would green-light almost any US event,
#  - SPORT words that live inside venue names. This one bit for real: reading
#    the whole `location` pulled `ufc` out of "UFC Apex, Las Vegas", and `ufc`
#    is in EVERY faceoff title, so a Vegas card matched Oklahoma City's video.
#    `test_match_vegas_event_rejects_other_citys_faceoff` caught it.
_PLACE_STOPWORDS = frozenset(
    {
        "ufc", "mma", "fight", "fights", "night", "octagon",
        "city", "the", "las", "los", "san", "de", "el", "st", "new",
        "arena", "center", "centre", "stadium", "coliseum", "hall", "garden",
        "gardens", "forum", "dome", "pavilion", "park", "place", "complex",
        "casino", "resort", "hotel", "theater", "theatre", "sports", "field",
        "palace", "expo", "convention", "entertainment", "grounds",
        "usa", "united", "states", "kingdom", "america", "american",
        # 🪤 Del recuento previo al backfill: el 1059 es "National Gymnastics
        # Arena, Baku, Azerbaijan" y "arena" ya estaba, pero "national" y
        # "gymnastics" no, asi que entraban como si fueran el nombre del sitio.
        # "national" es la palabra generica de cientos de recintos, y con la
        # ventana de 45 dias el riesgo era casi nulo; con los ~210 dias que
        # pide el backfill del historico, un video cualquiera con "face-off" y
        # "national" en el titulo se le escribiria encima — y la escritura es
        # first-writer-wins, o sea irreversible. Quitandolas, al 1059 le quedan
        # "baku" y "azerbaijan", que son los tokens que de verdad lo nombran:
        # no pierde nada y cierra un falso positivo.
        "national", "gymnastics",
    }
)


def _place_tokens(location: str | None) -> set[str]:
    """Distinctive, whole-word place tokens for the city guard.

    READS THE WHOLE `location`, NOT ONE COMMA FIELD — and that IS the fix
    (2026-08-02). The old version took the 2nd field as "the city", which the
    real data contradicts twice over:

      'Belgrade Arena, BG, Serbia'      -> 2nd field 'BG', 2 chars, dropped by
                                           the length cut -> EMPTY SET, and
                                           `any()` over an empty set is False
                                           ALWAYS. The 1063 faceoff was lost
                                           this way and had to be matched by
                                           hand; 'Washington, DC, USA' is the
                                           same shape.
      'London, England, United Kingdom' -> 2nd field is the STATE/REGION, so
                                           the token was 'england' and a video
                                           titled "UFC London ... Faceoffs"
                                           never matched. Same for
                                           'Miami, Florida, USA' and 8 more.

    The 31-jul patch proposed raising the length cut to 4 instead. That attacks
    the wrong thing and has a counterexample: 'Rio de Janeiro' would lose 'rio'.
    The cut STAYS at 3; what changes is where the words come from.

    UFC titles Las Vegas / Apex Fight Nights "UFC Vegas NNN: Fighter Faceoffs"
    (not "Las Vegas"), and some rows carry 'Nevada' as the city, so any card in
    Nevada / the Apex also accepts the 'vegas' token.
    """
    palabras = (t.strip(".") for t in _norm(location).replace(",", " ").split())
    tokens = {t for t in palabras if len(t) >= 3 and t not in _PLACE_STOPWORDS}
    full = _norm(location)
    if "vegas" in full or "nevada" in full or "apex" in full:
        tokens.add("vegas")
    return tokens


# 🪤 EL GUARD NO SABIA LEER "Noche UFC", Y ESO ERA UNA VELADA AL AÑO PERDIDA.
#
# Hasta hoy el token distintivo salía SOLO de `location`. Para el 1088 ("Noche
# UFC: Silva vs. Delgado", Glendale AZ) eso da {desert, diamond, glendale} — y
# la UFC titula ESTOS vídeos con la marca, nunca con la ciudad. Los dos únicos
# careos de una Noche UFC que hay en el canal en 9 años lo confirman: "Noche
# UFC: Fighter Faceoffs" (7dhhFNCQEcQ, 2025) y "Noche UFC: Weigh-In Faceoffs"
# (qpjPPGtcS3I, 2023). Cero tokens de lugar y cero "ufc <N>": se rechazaban, y
# como la escritura es first-writer-wins el careo se quedaba a NULL para
# siempre.
#
# La marca SÍ está en `events.name`. Pero el nombre trae además los APELLIDOS
# del estelar, y meterlos enteros era el desastre: medido sobre las 797 filas
# reales, el nombre completo suelta 760 tokens distintos en 790 filas, que
# aceptarían 647 pares (token, careo) contra los 483 títulos de careo que el
# canal publicó en 9 años. El peor es "fighter" (28 filas de The Ultimate
# Fighter): está en 108 de esos 483, porque el formato canónico ES "UFC X:
# Fighter Face-offs". Por eso aquí se recorta CUATRO veces:
#   - solo la MARCA, lo que va antes de ':' / raya (fuera los apellidos),
#   - NADA si esa cabecera es en realidad un emparejamiento ("Ortiz vs Shamrock
#     3: The Final Chapter", o una velada que llegue sin separador): ahí los
#     "tokens de marca" serían apellidos, y esto prefiere fallar por defecto,
#   - fuera los dígitos sueltos: el número ya lo cubre "ufc <N>", y más fino,
#   - y NADA si el evento ya tiene "ufc <N>", que además desactiva los nombres
#     patrocinados ("Crypto.com UFC 331", "Polymarket UFC 334").
# Resultado sobre las 797 filas reales: 3 tokens en 3 eventos (noche, freedom,
# macao) y 3 pares (token, careo) posibles en 9 años, los 3 correctos. De los 12
# eventos que el cron alcanza hoy, el único con token de marca es el 1088.
_BRAND_SPLIT_RE = re.compile(r"[:–—]| - ")
_BRAND_WORD_RE = re.compile(r"[^a-z0-9]+")
_BRAND_VS_RE = re.compile(r"\bvs\b")
# A las de lugar se suman las palabras que viven en los TÍTULOS de careo o en la
# nomenclatura de la marca, y que por tanto no distinguen una velada de otra.
# "fighter", "final" y "finale" son las que muerden: "UFC X: Fighter Face-offs"
# y el "Canelo vs Crawford: Final Faceoffs" (boxeo, 13-sep-2025) del canal, que
# cae DENTRO de la ventana de una Noche UFC de septiembre. "fox" y "fuel" son
# los 33 "UFC on FOX / FUEL TV" de 2011-2013: el cron no los alcanza, pero el
# backfill histórico de ~210 días que contempla este módulo sí, y ninguno de
# esos 33 tiene careo en YouTube, así que taparlos no cuesta nada.
_NAME_STOPWORDS = _PLACE_STOPWORDS | frozenset(
    {
        "fighter", "fighters", "faceoff", "faceoffs", "face", "offs",
        "prelims", "main", "card", "final", "finale", "ultimate", "team",
        "live", "road", "for", "com", "tv", "season", "presents", "and",
        "fox", "fuel",
    }
)


def _name_tokens(name: str | None) -> set[str]:
    """Distinctive BRAND tokens of the event NAME — the 'Noche UFC' guard.

    Only the brand half (before the first ':' / dash), so the main-event
    surnames never become tokens; nothing at all when that half is itself a
    matchup ("A vs B"); no bare digits; and NOTHING when the name already
    carries a "ufc <N>", because that card is guarded by its number, which is
    tighter, and it keeps sponsor words out ("Crypto.com UFC 331").
    """
    if _UFC_NUM_RE.search(name or ""):
        return set()
    marca = _BRAND_SPLIT_RE.split(_norm(name), 1)[0]
    if _BRAND_VS_RE.search(marca):
        return set()
    return {
        t
        for t in _BRAND_WORD_RE.split(marca)
        if len(t) >= 3 and not t.isdigit() and t not in _NAME_STOPWORDS
    }


def _title_has_place_token(title_n: str, tokens: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(tok)}\b", title_n) for tok in tokens)


def faceoff_candidate_ids(*feeds: list[FeedVideo] | None) -> list[str]:
    """Ids of every video whose title passes the face-off whitelist.

    This is the ONLY set worth spending a videos.list quota unit on: the title
    guard has already thrown out the other ~99% of the feed, so the duration
    lookup stays at one batch (1 unit) even across the RSS and rescue feeds."""
    ids: list[str] = []
    for feed in feeds:
        for video in feed or []:
            if _FACEOFF_RE.search(_norm(video.title)):
                ids.append(video.video_id)
    return list(dict.fromkeys(ids))


def _title_for(video_id: str, *feeds: list[FeedVideo] | None) -> str | None:
    """The video's own title, as YouTube published it, for migration 029.

    The title is already in the feed we matched against, so this costs nothing
    extra -- no second API call, no quota. Returns None when the id is not in
    any feed, and then the column simply stays NULL: a missing title is fine,
    an invented one is not.
    """
    for feed in feeds:
        for video in feed or ():
            if video.video_id == video_id:
                return video.title or None
    return None


def match_event(
    event: TargetEvent,
    videos: list[FeedVideo],
    durations: dict[str, int] | None = None,
) -> str | None:
    """First feed video that satisfies the conservative guard, else None.

    ``durations`` maps video id -> seconds (from fetch_video_durations). None,
    or an id that isn't in it, disables the length guard for that video — see
    _duration_ok for why the degraded path is deliberately permissive."""
    if event.event_date is None:
        return None
    place_tokens = _place_tokens(event.location)
    name_tokens = _name_tokens(event.name)
    num_match = _UFC_NUM_RE.search(event.name or "")
    ufc_num = num_match.group(1) if num_match else None
    lo = event.event_date - timedelta(days=_DAYS_BEFORE)
    hi = event.event_date + timedelta(days=_DAYS_AFTER)
    for video in videos:
        if not (lo <= video.published <= hi):
            continue
        title_n = _norm(video.title)
        if not _FACEOFF_RE.search(title_n):
            continue
        city_ok = _title_has_place_token(title_n, place_tokens)
        num_ok = bool(
            ufc_num
            and re.search(_TITLE_NUM_TEMPLATE.format(num=ufc_num), title_n)
        )
        brand_ok = _title_has_place_token(title_n, name_tokens)
        if not (city_ok or num_ok or brand_ok):
            continue
        if not _duration_ok(video.video_id, durations):
            LOGGER.info(
                "Descartado %s %r: %ds < %ds (parece un short, no un careo).",
                video.video_id,
                video.title,
                (durations or {}).get(video.video_id, -1),
                MIN_DURATION_SECONDS,
            )
            continue
        return video.video_id
    return None


def get_target_events(connection) -> list[TargetEvent]:
    """Upcoming events (from ~2 days ago onward) that still lack a face-off."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name, location, event_date
            FROM events
            WHERE status = 'upcoming'
              AND event_date IS NOT NULL
              AND event_date >= CURRENT_DATE - INTERVAL '2 days'
              AND faceoff_video_id IS NULL
            ORDER BY event_date ASC
            """
        )
        return [
            TargetEvent(int(r[0]), str(r[1]), r[2], r[3]) for r in cursor.fetchall()
        ]


def get_rescue_target_events(
    connection, lookback_days: int = _RESCUE_LOOKBACK_DAYS
) -> list[TargetEvent]:
    """Recently-passed / imminent events still missing a face-off, for the API
    rescue pass. Unlike get_target_events this does NOT require status='upcoming'
    (a passed event's careo is exactly what the RSS window can no longer reach),
    but stays bounded to the last ``lookback_days`` so we never chase careos for
    old cards that never had one."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name, location, event_date
            FROM events
            WHERE event_date IS NOT NULL
              AND event_date >= CURRENT_DATE - make_interval(days => %s)
              AND event_date <= CURRENT_DATE + INTERVAL '1 day'
              AND faceoff_video_id IS NULL
            ORDER BY event_date DESC
            """,
            (lookback_days,),
        )
        return [
            TargetEvent(int(r[0]), str(r[1]), r[2], r[3]) for r in cursor.fetchall()
        ]


def run(
    connection,
    *,
    apply: bool,
    feed: list[FeedVideo],
    api_feed: list[FeedVideo] | None = None,
    rescue_days: int = _RESCUE_LOOKBACK_DAYS,
    durations: dict[str, int] | None = None,
) -> Counter:
    counts: Counter = Counter()
    matched_ids: set[int] = set()

    # Pass 1 — RSS (free, no quota): upcoming events within the ~15-video window.
    events = get_target_events(connection)
    counts["targets"] = len(events)
    for event in events:
        video_id = match_event(event, feed, durations)
        if not video_id:
            counts["no_match"] += 1
            continue
        counts["matched"] += 1
        matched_ids.add(event.id)
        LOGGER.info("Event %d %r -> face-off %s (rss)", event.id, event.name, video_id)
        title = _title_for(video_id, feed)
        if apply and set_event_faceoff_video(connection, event.id, video_id, title):
            connection.commit()
            counts["written"] += 1

    # Pass 2 — YouTube Data API rescue (opt-in): recently-passed events whose
    # careo already dropped out of the RSS window. Same conservative guard.
    if api_feed is not None:
        rescue_events = get_rescue_target_events(connection, rescue_days)
        counts["rescue_targets"] = len(rescue_events)
        for event in rescue_events:
            if event.id in matched_ids:
                continue  # RSS already filled it this run
            video_id = match_event(event, api_feed, durations)
            if not video_id:
                counts["no_match"] += 1
                continue
            counts["rescued"] += 1
            matched_ids.add(event.id)
            LOGGER.info(
                "Event %d %r -> face-off %s (api rescue)",
                event.id,
                event.name,
                video_id,
            )
            title = _title_for(video_id, api_feed, feed)
            if apply and set_event_faceoff_video(
                connection, event.id, video_id, title
            ):
                connection.commit()
                counts["written"] += 1
    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Match UFC face-off (careo) videos to events (RSS + API rescue)."
    )
    parser.add_argument("--apply", action="store_true", help="Persist matches (default: dry-run).")
    parser.add_argument("--dump-feed", action="store_true", help="Print the channel RSS feed and exit (no DB).")
    parser.add_argument(
        "--rescue-days", type=int, default=_RESCUE_LOOKBACK_DAYS, dest="rescue_days",
        help="API rescue look-back window in days (needs YOUTUBE_API_KEY). Default 45.",
    )
    # El tope de paginacion, expuesto porque un backfill lo necesita MAYOR y
    # hasta ahora estaba clavado en el modulo. El default no se toca: el cron
    # diario mira 45 dias y con 20 paginas le sobra. Quien barra el historico
    # pide mas — y si se queda corto, ahora el WARNING de fetch_channel_uploads
    # se lo dice con la fecha a la que llego de verdad.
    parser.add_argument(
        "--max-pages", type=int, default=_UPLOADS_MAX_PAGES, dest="max_pages",
        help=f"Cap on uploads pages (50 videos each, 1 quota unit). Default {_UPLOADS_MAX_PAGES}.",
    )
    args = parser.parse_args()

    session = requests.Session()
    feed = fetch_channel_feed(session)
    if args.dump_feed:
        for video in feed:
            print(f"{video.video_id} | {video.published} | {video.title}")
        return
    if not feed:
        raise SystemExit("Empty channel feed — aborting (no writes).")

    # Optional API rescue: only when a quota key is present. Without it the cron
    # behaves exactly as before (RSS only) — graceful, zero-cost degradation.
    api_feed: list[FeedVideo] | None = None
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if api_key:
        # El cutoff se DERIVA de _DAYS_BEFORE, asi que estrecharlo de 5 a 2 no
        # rompe el rescate: el evento mas viejo que get_rescue_target_events
        # devuelve esta en `today - rescue_days`, su ventana de match empieza en
        # `today - rescue_days - _DAYS_BEFORE`, y este cutoff sigue quedando 2
        # dias POR DEBAJO de ese suelo. Las dos cifras se mueven juntas. Y no se
        # pierde ningun careo real: el desfase maximo medido sobre los 30 de la
        # base es de 1 dia (ver _DAYS_BEFORE).
        cutoff = date.today() - timedelta(days=args.rescue_days + _DAYS_BEFORE + 2)
        try:
            api_feed = fetch_channel_uploads(
                api_key, published_after=cutoff, max_pages=args.max_pages
            )
            # 🪤 Este log decia «since <cutoff>» — la fecha PEDIDA, no la
            # alcanzada. Con el tope agotado imprimia exactamente lo mismo que
            # en una corrida completa, asi que ni siquiera leyendo el log se
            # podia saber si el feed estaba entero.
            alcanzado = min((v.published for v in api_feed), default=None)
            LOGGER.info(
                "API rescue: %d uploads, pedido desde %s, alcanzado %s (max_pages=%d)",
                len(api_feed), cutoff, alcanzado, args.max_pages,
            )
        except Exception as exc:  # noqa: BLE001 - never let rescue break the RSS run
            LOGGER.warning("API rescue fetch failed (%s); continuing RSS-only.", exc)
            api_feed = None
    else:
        LOGGER.info("No YOUTUBE_API_KEY: RSS-only (no API rescue).")

    # Duration guard. Only the titles that ALREADY passed the face-off whitelist
    # are worth a lookup, so this is one batch (1 quota unit) per run. Without a
    # key `durations` stays None and the guard degrades to open on purpose —
    # _duration_ok explains why that beats rejecting everything in silence.
    durations: dict[str, int] | None = None
    if api_key:
        candidatos = faceoff_candidate_ids(feed, api_feed)
        durations = fetch_video_durations(api_key, candidatos)
        LOGGER.info(
            "Duracion: %d candidatos con 'face-off' en el titulo, %d resueltos "
            "(umbral %ds).",
            len(candidatos), len(durations), MIN_DURATION_SECONDS,
        )
    else:
        LOGGER.warning(
            "Sin YOUTUBE_API_KEY no hay filtro de duracion: un short con el "
            "titulo adecuado solo lo paran la ventana de %d dias y el patron "
            "del numero de cartelera.",
            _DAYS_BEFORE,
        )

    settings = get_settings()
    with connect(settings.database_url) as connection:
        counts = run(
            connection, apply=args.apply, feed=feed,
            api_feed=api_feed, rescue_days=args.rescue_days,
            durations=durations,
        )

    keys = ["targets", "matched", "rescue_targets", "rescued", "no_match", "written"]
    print(json.dumps({k: counts.get(k, 0) for k in keys}, indent=2))
    if not args.apply:
        print("Dry-run: nothing written. Re-run with --apply to persist.")


if __name__ == "__main__":
    main()
