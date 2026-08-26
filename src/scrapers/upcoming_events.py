"""Scrape upcoming UFC events (with full fight cards) from ufc.com/events.

ufcstats only publishes completed events (and main.py filters <=2025), so the DB
has zero upcoming events. This reads ufc.com/events (server-rendered, same source
used for rankings; ufcespanol.com returns Varnish 403), which is a two-level site:
  - the listing (#events-list-upcoming) gives each event's slug, headliner, start
    timestamps, venue/location and ticket link, but only "LastName vs LastName" bout
    labels;
  - each event detail page gives the full card (full fighter names, weight class,
    segment, order) plus poster image, broadcast, tagline and the per-section
    start times (Main Card / Prelims / Early Prelims broadcaster blocks -> BE6:
    events.start_time / prelims_time / early_prelims_time, migration 011).

Events are inserted with status='upcoming' and their bouts with NULL results
(winner/method/end_*). Fighters are matched to `fighters` via espn.py's matcher
(+ rankings' diacritic fallback); fighter_red_name/fighter_blue_name are ALWAYS
filled (like rankings.fighter_name) so the frontend can render unmatched fighters.

Idempotent: events upsert by (source, source_id); each event's bouts upsert by
their ufc.com fmid, bouts that dropped off the card are marked status='cancelled'
(never deleted — a reappearing bout is reactivated by the upsert), and upcoming
events that have dropped off the ufc.com list are completed once their date passes.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .config import Settings, get_settings
from .db import connect
from .enrich_photos_ufc import _is_placeholder_image, _normalize_ufc_url
from .espn import _build_exact_name_index, _build_normalized_name_index, _match_fighter
from .logging_config import configure_logging
from .rankings import BROWSER_HEADERS, _build_folded_index, _match_fighter_folded
from .repositories.events import EventMetaRecord, upsert_event_meta
from .repositories.fights import (
    UpcomingFightRecord,
    cancel_missing_upcoming_fights,
    reconcile_upcoming_fight_source_id,
    upsert_upcoming_fight,
)
from .repositories.fighters import (
    get_all_fighters,
    update_fighter_standing_photo,
    update_fighter_standing_variant,
)


LOGGER = logging.getLogger(__name__)

SOURCE = "ufc.com"
EVENTS_URL = "https://www.ufc.com/events"
HOME_URL = "https://www.ufc.com/"

# ufc.com pagina el listado de 8 en 8 y hay que recorrerlo entero.
#
# EL FALLO QUE ESTO TAPA, medido el 26-ago-2026: pediamos SOLO /events, o sea la
# primera pagina, y la UFC tenia 12 eventos anunciados. Cuatro nos eran
# invisibles: ufc-333 (que ya estaba en la base y por eso dejo de refrescarse en
# cuanto un evento nuevo lo empujo fuera), y ufc-fight-night-november-07-2026,
# ufc-334 y ufc-335, que NUNCA habian entrado. Es una averia que empeora sola:
# cada evento nuevo que anuncia la UFC expulsa a otro del listado que miramos.
#
# 🪤 EL BOTON "LOAD MORE" DE ufc.com NO SIRVE COMO CRITERIO DE PARADA. La misma
# pagina arrastra ademas la lista de eventos PASADOS, asi que ?page=2 devuelve 0
# proximos pero sigue trayendo 8 pasados y sigue ofreciendo ?page=3, hacia atras
# hasta 1993. Un bucle que siga ese boton se descarga el archivo historico
# entero. Se para por "0 tarjetas dentro de #events-list-upcoming", que es lo
# unico que significa "no hay mas eventos futuros".
#
# El tope no es decorativo: ufc.com responde a ?page=lo-que-sea con la pagina 0,
# asi que una URL mal construida no veria nunca una pagina vacia.
MAX_LISTING_PAGES = 6

# ufc.com event detail pages come in two templates: near-term events wrap segments in
# div.main-card / div.fight-card-prelims / div.fight-card-early-prelims; far-out events
# list every bout in a single undifferentiated "Fight Card" list (UFC hasn't split the
# card yet). We therefore read bouts in document order (authoritative for bout_order)
# and resolve card_segment from wrappers/labels when present, else leave it NULL.
SEGMENT_WRAPPER_CLASSES = {
    "main-card": "main",
    "fight-card-prelims": "prelims",
    "fight-card-early-prelims": "early_prelims",
}

# Drupal image style of the standing full-body photo each bout view serves per
# corner (PNG alpha, ~185x600+). Only URLs carrying this marker are captured
# into fighters.standing_body_url (migration 009); anything else in the corner
# (flag icons, ranking badges, another style) is ignored.
STANDING_IMAGE_STYLE = "event_fight_card_upper_body_of_standing_athlete"

# F1 Tanda 4: the card image URL carries a corner token (..._L_MM-DD.png /
# ..._R_MM-DD.png). Red corner -> _L_ (faces right), blue -> _R_ (faces left).
# Each direction is stored in its own column (migration 019) so the face-off
# picks the correctly-facing variant per corner.
_STANDING_TOKEN_RE = re.compile(r"_([LR])_\d")


def _standing_direction(image_url: str | None) -> str | None:
    """'L' or 'R' from the ufc.com card URL token, or None if absent."""
    if not image_url:
        return None
    match = _STANDING_TOKEN_RE.search(image_url)
    return match.group(1) if match else None


@dataclass
class ParsedBout:
    card_segment: str | None
    bout_order: int
    weight_class: str | None
    scheduled_rounds: int | None
    red_name: str
    blue_name: str
    fmid: str
    # Standing full-body photos from the bout view (optional: far-out cards and
    # debut fighters have no photo yet). Defaults keep old constructions valid.
    red_image_url: str | None = None
    blue_image_url: str | None = None
    # "Title Bout" flag from the class text; already used for scheduled_rounds
    # and now persisted into fights.is_title_fight (migration 010).
    is_title: bool = False


@dataclass
class ParsedEvent:
    source_id: str          # ufc.com slug
    detail_url: str
    headliner: str | None
    event_date: date | None
    start_time: datetime | None
    location: str | None
    ticket_url: str | None
    name: str | None = None
    image_url: str | None = None
    broadcast: str | None = None
    tagline: str | None = None
    bouts: list[ParsedBout] = field(default_factory=list)
    # Section start times from the detail page's broadcaster blocks (BE6).
    # None = the section is not announced (far-out cards) and must never
    # overwrite a stored value (COALESCE in upsert_event_meta).
    prelims_time: datetime | None = None
    early_prelims_time: datetime | None = None


# --------------------------------------------------------------------------- fetch


def _get_soup(session: requests.Session, url: str, settings: Settings) -> BeautifulSoup:
    time.sleep(settings.request_delay_seconds)
    response = session.get(url, timeout=settings.request_timeout_seconds)
    response.raise_for_status()
    response.encoding = "utf-8"
    return BeautifulSoup(response.text, "lxml")


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    return session


# ------------------------------------------------------------------------ listing


def _parse_listing(soup: BeautifulSoup) -> list[ParsedEvent]:
    container = soup.select_one("#events-list-upcoming") or soup
    events: list[ParsedEvent] = []
    for card in container.select(".c-card-event--result"):
        link = card.select_one(".c-card-event--result__logo a[href], a[href*='/event/']")
        if link is None:
            continue
        href = link.get("href", "")
        slug = href.rstrip("/").split("/event/")[-1].split("#")[0].split("?")[0]
        if not slug:
            continue
        headline_el = card.select_one(".c-card-event--result__headline")
        headliner = headline_el.get_text(strip=True) if headline_el else None
        start_time, event_date = _parse_card_datetime(card)
        events.append(
            ParsedEvent(
                source_id=slug,
                detail_url=urljoin(HOME_URL, f"/event/{slug}"),
                headliner=headliner,
                event_date=event_date,
                start_time=start_time,
                location=_parse_card_location(card),
                ticket_url=_parse_card_ticket(card),
            )
        )
    return events


def _parse_all_listing_pages(session, settings, counts: Counter) -> list[ParsedEvent]:
    """Recorre el listado de ufc.com pagina a pagina hasta agotar los futuros.

    Envuelve a `_parse_listing`, que no se toca: esta funcion solo decide QUE
    paginas se le dan de comer. Ver el comentario de MAX_LISTING_PAGES arriba
    para el porque y para la trampa del boton "Load More".

    Deja tres contadores en `counts`, y no son adorno: si el dia de manana la UFC
    cambia la paginacion, `listing_pages_fetched: 1` en el log del cron lo delata
    al instante en vez de que esto falle en silencio, que es como empezo todo.
    """
    vistos: set[str] = set()
    eventos: list[ParsedEvent] = []
    for numero in range(MAX_LISTING_PAGES):
        url = EVENTS_URL if numero == 0 else f"{EVENTS_URL}?page={numero}"
        try:
            soup = _get_soup(session, url, settings)
        except Exception as exc:
            # Una pagina caida NO puede parecer "ya no hay mas eventos": eso
            # marcaria como desaparecidos los que vinieran detras. Se corta el
            # bucle y se deja constancia; quien decide que hacer es el guard de
            # `_complete_dropped_upcoming`.
            counts["listing_pages_failed"] += 1
            LOGGER.warning("Listing page %s failed: %s", url, exc)
            break

        # Se exige el contenedor de PROXIMOS. Si no esta, no se parsea: la misma
        # pagina trae tambien los eventos pasados, y `_parse_listing` cae a
        # `or soup` cuando falta el contenedor -- se colarian como futuros.
        if soup.select_one("#events-list-upcoming") is None:
            break

        pagina = _parse_listing(soup)
        counts["listing_pages_fetched"] += 1
        if not pagina:
            break
        for evento in pagina:
            if evento.source_id in vistos:
                continue
            vistos.add(evento.source_id)
            eventos.append(evento)
    else:
        # Se agoto el tope sin encontrar una pagina vacia: no sabemos si falta
        # algo detras, asi que tampoco se puede dar nada por desaparecido.
        counts["listing_cap_hit"] = 1
        LOGGER.warning("Listing cap de %s paginas alcanzado", MAX_LISTING_PAGES)
    return eventos


def _parse_card_datetime(card) -> tuple[datetime | None, date | None]:
    date_el = card.select_one(".c-card-event--result__date")
    if date_el is None:
        return None, None
    ts_raw = date_el.get("data-main-card-timestamp") or date_el.get("data-prelims-card-timestamp")
    start_time: datetime | None = None
    if ts_raw and ts_raw.isdigit():
        start_time = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
    # Prefer the displayed local date (ET) for event_date; fall back to UTC date.
    label = date_el.get("data-main-card") or date_el.get_text(" ", strip=True)
    event_date = _parse_display_date(label, start_time)
    if event_date is None and start_time is not None:
        event_date = start_time.date()
    return start_time, event_date


def _parse_display_date(label: str | None, start_time: datetime | None) -> date | None:
    if not label:
        return None
    # e.g. "Sat, Jun 20 / 8:00 PM EDT" -> month/day (ET calendar date) after the weekday comma.
    after_comma = label.split(",", 1)[-1]
    match = re.search(r"([A-Za-z]{3})\s+(\d{1,2})", after_comma)
    if not match:
        return None
    try:
        month = datetime.strptime(match.group(1), "%b").month
    except ValueError:
        return None
    day = int(match.group(2))
    # The label has no year. start_time is the UTC instant, whose year can differ from the
    # ET calendar year at the Dec/Jan boundary (a US-evening event rolls into next-day UTC).
    # Reconcile: if the label says December but UTC is already January, the ET year is one less.
    base = start_time or datetime.now(tz=timezone.utc)
    year = base.year
    if start_time is not None:
        if month == 12 and start_time.month == 1:
            year -= 1
        elif month == 1 and start_time.month == 12:
            year += 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_card_location(card) -> str | None:
    venue_el = card.select_one("h5")
    parts = [venue_el.get_text(strip=True)] if venue_el else []
    for cls in (".locality", ".administrative-area", ".country"):
        el = card.select_one(cls)
        if el and el.get_text(strip=True):
            parts.append(el.get_text(strip=True))
    parts = [p for p in parts if p]
    return ", ".join(parts) or None


def _parse_card_ticket(card) -> str | None:
    buttons = card.select("a.e-button--white[href], a[href]")
    http_links = [a for a in buttons if a.get("href", "").startswith("http")]
    for a in http_links:
        if "ticket" in a.get_text(" ", strip=True).lower():
            return a["href"]
    return http_links[0]["href"] if http_links else None


# ------------------------------------------------------------------------- detail


def _parse_detail(soup: BeautifulSoup, event: ParsedEvent) -> None:
    event.name = _build_event_name(soup, event.headliner)
    event.tagline = _meta_content(soup, "og:description")
    event.image_url = _parse_detail_image(soup)
    event.broadcast = _derive_broadcast(event.name)
    event.bouts = _parse_bouts(soup, event.source_id)
    section_times = _parse_section_times(soup)
    event.prelims_time = section_times.get("prelims")
    event.early_prelims_time = section_times.get("early_prelims")
    # The listing already provides the main-card start; the detail page's own
    # "Main Card" block fills it only when the listing timestamp was missing.
    if event.start_time is None:
        event.start_time = section_times.get("main")


def _parse_section_times(soup: BeautifulSoup) -> dict[str, datetime]:
    """Per-section start times from the event detail page (BE6).

    ufc.com renders one broadcaster block per announced section:

        <div class="c-event-fight-card-broadcaster__container">
          <div class="c-event-fight-card-broadcaster__card-title">
            <strong>Main Card</strong>   (or "Prelims" / "Early Prelims")
          ...
          <div class="c-event-fight-card-broadcaster__time tz-change-inner"
               data-timestamp="1783818000" ...>
            ... <time datetime="2026-07-11T21:00:00Z">Sat, Jul 11 / 9:00 PM</time>

    ONLY the epoch data-timestamp is trusted (same convention as the listing's
    data-main-card-timestamp). The inner <time datetime="...Z"> is NOT a
    fallback: on the real page it carries the ET local time mislabeled with a
    "Z" suffix (verified on /event/ufc-329: data-timestamp 1783818000 =
    2026-07-12T01:00Z = 9:00 PM EDT, while the attribute claims
    "2026-07-11T21:00:00Z"), so parsing it as UTC would shift every section
    by 4-5 hours. Sections not yet announced (far-out cards) simply have no
    block; the wrapper duplicates each block (desktop/mobile), so the dict
    keeps the first occurrence per segment.
    """
    times: dict[str, datetime] = {}
    for container in soup.select(".c-event-fight-card-broadcaster__container"):
        title = container.select_one(".c-event-fight-card-broadcaster__card-title")
        segment = _segment_from_text(title.get_text(" ", strip=True) if title else None)
        if segment is None or segment in times:
            continue
        time_el = container.select_one(".c-event-fight-card-broadcaster__time")
        ts_raw = (time_el.get("data-timestamp") or "") if time_el else ""
        if ts_raw.isdigit():
            times[segment] = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
    return times


def _build_event_name(soup: BeautifulSoup, headliner: str | None) -> str | None:
    title = _meta_content(soup, "og:title") or ""
    series = title.replace("| UFC", "").strip(" |") or "UFC"
    if not headliner:
        return series
    headliner_vs = re.sub(r"\bvs\b(?!\.)", "vs.", headliner)
    return f"{series}: {headliner_vs}"


def _derive_broadcast(name: str | None) -> str | None:
    """Return an ESTIMATED broadcaster for an upcoming event.

    IMPORTANT: this value is a HEURISTIC, not a scraped fact. ufc.com exposes no
    reliable per-event broadcaster in static HTML, so we apply UFC's standard
    distribution model purely from the event NAME:
      - numbered events ("UFC 329") are pay-per-view              -> "PPV"
      - everything else (Fight Night, UFC on ESPN/ABC) streams on -> "ESPN+ / Fight Pass"
    It can be wrong (e.g. UFC on ABC, region-specific deals, schedule changes), so
    the frontend must label it as an ESTIMATION (see mma-app event detail, where it is
    rendered as "Emisión: <value>" with an "estimada" badge). The stored string itself
    is left clean (no marker) so the existing `events.broadcast` column/schema is
    unchanged; the "estimated" semantics live in this docstring + the UI label.
    """
    if not name:
        return None
    if re.search(r"\bUFC\s+\d+\b", name):
        return "PPV"
    return "ESPN+ / Fight Pass"


def _meta_content(soup: BeautifulSoup, prop: str) -> str | None:
    el = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    content = el.get("content").strip() if el and el.get("content") else None
    return content or None


def _parse_detail_image(soup: BeautifulSoup) -> str | None:
    for img in soup.select("img[src]"):
        src = img["src"]
        if "background_image" in src:
            return src
    return _meta_content(soup, "og:image")


def _parse_bouts(soup: BeautifulSoup, event_source_id: str) -> list[ParsedBout]:
    bouts: list[ParsedBout] = []
    for bout in soup.select(".c-listing-fight"):
        red = _corner_name(bout, "red")
        blue = _corner_name(bout, "blue")
        if not red and not blue:
            continue
        order = len(bouts) + 1
        class_text = _bout_class_text(bout)
        is_title = bool(class_text and re.search(r"\bTitle\b", class_text, re.IGNORECASE))
        fmid = bout.get("data-fmid") or ""
        bouts.append(
            ParsedBout(
                card_segment=_bout_segment(bout),
                bout_order=order,
                weight_class=_clean_weight_class(class_text),
                scheduled_rounds=5 if (order == 1 or is_title) else 3,
                red_name=red or "TBD",
                blue_name=blue or "TBD",
                # data-fmid is a globally-unique UFC fight id; the fallback is unique per event.
                fmid=fmid if fmid else f"{event_source_id}#{order}",
                red_image_url=_corner_image(bout, "red"),
                blue_image_url=_corner_image(bout, "blue"),
                is_title=is_title,
            )
        )
    return bouts


def _corner_image(bout, color: str) -> str | None:
    """Standing full-body photo URL for a corner, or None.

    The bout view serves it as .c-listing-fight__corner-image--<color> img[src]
    with the athlete's name as alt. The host arrives as https://ufc.com (no www)
    -> normalized to https://www.ufc.com. Only the standing-athlete Drupal style
    is accepted; placeholders/silhouettes are rejected like every other photo.
    """
    img = bout.select_one(f".c-listing-fight__corner-image--{color} img[src]")
    if img is None:
        return None
    url = _normalize_ufc_url(img.get("src"))
    if not url or STANDING_IMAGE_STYLE not in url or _is_placeholder_image(url):
        return None
    return url


def _segment_from_text(text: str | None) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    if "early" in lowered:
        return "early_prelims"
    if "prelim" in lowered:
        return "prelims"
    if "main" in lowered:
        return "main"
    return None


def _bout_segment(bout) -> str | None:
    # Near-term template: a wrapper ancestor names the segment.
    for ancestor in bout.parents:
        classes = ancestor.get("class", []) if hasattr(ancestor, "get") else []
        for wrapper_class, segment in SEGMENT_WRAPPER_CLASSES.items():
            if wrapper_class in classes:
                return segment
    # Labeled template: nearest preceding card-title heading.
    title = bout.find_previous(class_="c-event-fight-card-broadcaster__card-title")
    if title is not None:
        segment = _segment_from_text(title.get_text(" ", strip=True))
        if segment:
            return segment
    # Far-out events list all bouts undifferentiated -> segment unknown.
    return None


def _corner_name(bout, color: str) -> str | None:
    el = bout.select_one(f".c-listing-fight__corner-name--{color}")
    if el is None:
        return None
    given = el.select_one(".c-listing-fight__corner-given-name")
    family = el.select_one(".c-listing-fight__corner-family-name")
    if given or family:
        name = " ".join(
            part.get_text(strip=True)
            for part in (given, family)
            if part and part.get_text(strip=True)
        )
        if name:
            return name
    text = el.get_text(" ", strip=True)
    return text or None


def _bout_class_text(bout) -> str | None:
    el = bout.select_one(".c-listing-fight__class-text")
    return el.get_text(strip=True) if el else None


def _clean_weight_class(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = re.sub(r"\b(UFC|Interim|Title|Tournament|Bout)\b", " ", text, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split())
    return cleaned or None


# ----------------------------------------------------------------------- matching


def _make_matcher(fighters):
    exact_index = _build_exact_name_index(fighters)
    normalized_index = _build_normalized_name_index(fighters)
    folded_index = _build_folded_index(fighters)

    def match(name: str) -> int | None:
        if not name or name == "TBD":
            return None
        found = _match_fighter(name, exact_index, normalized_index)
        if found is None:
            found = _match_fighter_folded(name, folded_index)
        return found.id if found else None

    return match


# ----------------------------------------------------------------------- load


def _complete_dropped_upcoming(connection, current_source_ids: set[str]) -> int:
    """Mark ufc.com events that have dropped off the upcoming list as completed.

    An event leaves ufc.com's upcoming list once it has happened. The frontend splits
    events by DATE for "Pasados" (event_date < today) and by STATUS for "Próximos"
    (status = 'upcoming'), so flipping status 'upcoming' -> 'completed' moves a finished
    event out of Próximos and into Pasados on its own. The previous behaviour DELETED the
    event (and its bouts), so any 2026+ event the user had seen vanished forever — the
    ufcstats re-importer never brings it back (its year<=2025 cutoff drops it). Bouts are
    preserved with NULL results; a separate results backfill can fill winner/method later.

    IMPORTANT: dropping off the listing does NOT mean the event happened — ufc.com's
    page 1 shows only ~8 cards, so with 9+ events announced the furthest one falls off
    while still months away (event id=1085, Paris 2026-09-05, got flipped this way).
    The UPDATE therefore guards on event_date < CURRENT_DATE and the returned count
    sums cursor.rowcount (rows actually flipped), not len(stale_ids).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, source_id FROM events WHERE source = %s AND status = 'upcoming'",
            (SOURCE,),
        )
        rows = cursor.fetchall()
        stale_ids = [int(r[0]) for r in rows if str(r[1]) not in current_source_ids]
        completed = 0
        for event_id in stale_ids:
            cursor.execute(
                "UPDATE events SET status = 'completed' "
                "WHERE id = %s AND event_date IS NOT NULL AND event_date < CURRENT_DATE",
                (event_id,),
            )
            if cursor.rowcount == 0:
                # Dropped off the listing but not flipped (future or NULL date):
                # expected for far-out events pushed to page 2; log it so a
                # permanently stuck event (e.g. unparseable date) stays visible.
                LOGGER.info("Stale event %s not completed (event_date in the future or NULL)", event_id)
            completed += cursor.rowcount
        return completed


def _write_event_bouts(connection, match, counts: Counter, event: ParsedEvent, event_id: int) -> None:
    """Reconcile one event's card: upsert every scraped bout (keyed by fmid),
    then mark the bouts that are no longer on the card as status='cancelled'.

    The old behaviour deleted the event's fights wholesale and re-inserted them,
    which lost fight ids (and any row pointing at them) on every run and made a
    dropped bout vanish without a trace. Now:
      - each scraped bout upserts by (source, fmid); the conflict branch also
        resets status to NULL, so a previously-cancelled pairing that REAPPEARS
        on the card is reactivated;
      - the upserted row ids are the survivors: every other row of this
        (event, source) — i.e. every fighter pairing no longer scheduled — is
        flipped to 'cancelled' by cancel_missing_upcoming_fights (bouts with a
        result are never touched);
      - untouched bouts keep their id, results columns and photos logic intact.
    The frontend filters status='cancelled' wherever it counts or lists bouts.
    """
    kept_ids: list[int] = []
    for bout in event.bouts:
        red_id = match(bout.red_name)
        blue_id = match(bout.blue_name)
        # ufc.com's per-fight fmid is not stable across scrapes: adopt any
        # existing row of this (event, pairing) whose source_id drifted so the
        # upsert updates it in place instead of inserting a phantom duplicate
        # (a 'cancelled' twin sharing the bout_order — see event 1060).
        reconcile_upcoming_fight_source_id(
            connection, event_id, SOURCE, bout.fmid,
            red_id, blue_id, bout.red_name, bout.blue_name,
        )
        fight_id = upsert_upcoming_fight(
            connection,
            UpcomingFightRecord(
                event_id=event_id,
                fighter_red_id=red_id,
                fighter_blue_id=blue_id,
                fighter_red_name=bout.red_name,
                fighter_blue_name=bout.blue_name,
                weight_class=bout.weight_class,
                scheduled_rounds=bout.scheduled_rounds,
                bout_order=bout.bout_order,
                card_segment=bout.card_segment,
                source=SOURCE,
                source_id=bout.fmid,
                is_title_fight=bout.is_title,
            ),
        )
        kept_ids.append(fight_id)
        # Standing full-body photo: newest scrape wins (UFC refreshes
        # it after every fight); the repository never writes NULL/empty
        # over an existing value.
        for fighter_id, image_url in (
            (red_id, bout.red_image_url),
            (blue_id, bout.blue_image_url),
        ):
            if fighter_id is not None and image_url:
                if update_fighter_standing_photo(connection, fighter_id, image_url):
                    counts["standing_photos_updated"] += 1
                # F1: fill this corner's directional column (first-writer-wins).
                direction = _standing_direction(image_url)
                if direction and update_fighter_standing_variant(
                    connection, fighter_id, image_url, direction
                ):
                    counts["standing_directional_updated"] += 1
    counts["bouts_cancelled"] += cancel_missing_upcoming_fights(
        connection, event_id, SOURCE, kept_ids
    )


def scrape_upcoming_events(dry_run: bool = False) -> Counter:
    settings = get_settings()
    counts: Counter = Counter()
    session = _new_session()
    session.get(HOME_URL, timeout=settings.request_timeout_seconds)

    events = _parse_all_listing_pages(session, settings, counts)
    counts["events_found"] = len(events)

    for event in events:
        try:
            detail = _get_soup(session, event.detail_url, settings)
            _parse_detail(detail, event)
            counts["details_fetched"] += 1
            counts["bouts_parsed"] += len(event.bouts)
        except Exception as exc:
            counts["detail_errors"] += 1
            LOGGER.warning("Failed to fetch/parse detail for %s: %s", event.source_id, exc)

    with connect(settings.database_url) as connection:
        fighters = get_all_fighters(connection)
        match = _make_matcher(fighters)
        counts["fighters_in_db"] = len(fighters)

        for event in events:
            for bout in event.bouts:
                if match(bout.red_name) is not None:
                    counts["bouts_red_matched"] += 1
                if match(bout.blue_name) is not None:
                    counts["bouts_blue_matched"] += 1

        if dry_run:
            counts["events_written"] = 0
            _log_preview(events)
            return counts

        # ⚠️ LA LINEA MAS DELICADA DE LA PAGINACION. `_complete_dropped_upcoming`
        # da por terminado todo evento 'upcoming' que ya NO salga en el listado.
        # Con una sola pagina eso era una lista completa; con varias, una pagina
        # caida a medio recorrido deja fuera eventos que existen perfectamente, y
        # los marcaria como desaparecidos. Ante la duda no se cierra nada: un
        # evento de mas en "Proximos" durante un dia es barato; darlo por
        # terminado cuando no lo esta se ve en la portada.
        if counts["listing_pages_failed"] or counts["listing_cap_hit"]:
            counts["stale_skipped"] = 1
            LOGGER.warning(
                "Listado incompleto (failed=%s cap=%s): no se cierra ningun evento",
                counts["listing_pages_failed"],
                counts["listing_cap_hit"],
            )
        else:
            current_ids = {e.source_id for e in events}
            counts["stale_completed"] = _complete_dropped_upcoming(connection, current_ids)
        connection.commit()

        for event in events:
            try:
                record = EventMetaRecord(
                    name=event.name or event.headliner or event.source_id,
                    event_date=event.event_date,
                    start_time=event.start_time,
                    location=event.location,
                    promotion_id=settings.promotion_id_ufc,
                    status="upcoming",
                    image_url=event.image_url,
                    tagline=event.tagline,
                    broadcast=event.broadcast,
                    ticket_url=event.ticket_url,
                    headliner=event.headliner,
                    source=SOURCE,
                    source_id=event.source_id,
                    prelims_time=event.prelims_time,
                    early_prelims_time=event.early_prelims_time,
                )
                event_id = upsert_event_meta(connection, record)
                _write_event_bouts(connection, match, counts, event, event_id)
                connection.commit()
                counts["events_written"] += 1
                counts["bouts_written"] += len(event.bouts)
            except Exception:
                connection.rollback()
                counts["write_errors"] += 1
                LOGGER.exception("Failed to write event %s", event.source_id)
    return counts


def _log_preview(events: list[ParsedEvent]) -> None:
    for event in events:
        LOGGER.info(
            "[%s] %s | %s | %s | bouts=%s | broadcast=%s | ticket=%s",
            event.event_date, event.name, event.headliner, event.location,
            len(event.bouts), event.broadcast, bool(event.ticket_url),
        )


def _build_summary(counts: Counter) -> str:
    keys = [
        # Las tres de paginacion van las primeras a proposito: son la unica
        # senal de que el listado se esta leyendo entero. Un
        # `listing_pages_fetched: 1` en el log del cron significa que la
        # paginacion se rompio -- y esa averia es silenciosa, no da error.
        "listing_pages_fetched", "listing_pages_failed", "listing_cap_hit",
        "events_found", "details_fetched", "detail_errors", "bouts_parsed",
        "fighters_in_db", "bouts_red_matched", "bouts_blue_matched",
        "stale_completed", "stale_skipped", "events_written", "bouts_written",
        "bouts_cancelled", "standing_photos_updated", "write_errors",
    ]
    return json.dumps({key: counts.get(key, 0) for key in keys}, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape upcoming UFC events into events/fights.")
    parser.add_argument("--dry-run", action="store_true", help="Parse + match but do not write.")
    args = parser.parse_args()
    configure_logging()
    counts = scrape_upcoming_events(dry_run=args.dry_run)
    print(_build_summary(counts))


if __name__ == "__main__":
    main()
