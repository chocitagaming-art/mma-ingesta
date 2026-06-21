"""Scrape upcoming UFC events (with full fight cards) from ufc.com/events.

ufcstats only publishes completed events (and main.py filters <=2025), so the DB
has zero upcoming events. This reads ufc.com/events (server-rendered, same source
used for rankings; ufcespanol.com returns Varnish 403), which is a two-level site:
  - the listing (#events-list-upcoming) gives each event's slug, headliner, start
    timestamps, venue/location and ticket link, but only "LastName vs LastName" bout
    labels;
  - each event detail page gives the full card (full fighter names, weight class,
    segment, order) plus poster image, broadcast and tagline.

Events are inserted with status='upcoming' and their bouts with NULL results
(winner/method/end_*). Fighters are matched to `fighters` via espn.py's matcher
(+ rankings' diacritic fallback); fighter_red_name/fighter_blue_name are ALWAYS
filled (like rankings.fighter_name) so the frontend can render unmatched fighters.

Idempotent: events upsert by (source, source_id); each event's bouts are deleted
and re-inserted; upcoming events that have dropped off the ufc.com list are removed.
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
from .espn import _build_exact_name_index, _build_normalized_name_index, _match_fighter
from .logging_config import configure_logging
from .rankings import BROWSER_HEADERS, _build_folded_index, _match_fighter_folded
from .repositories.events import EventMetaRecord, upsert_event_meta
from .repositories.fights import (
    UpcomingFightRecord,
    delete_upcoming_fights,
    upsert_upcoming_fight,
)
from .repositories.fighters import get_all_fighters


LOGGER = logging.getLogger(__name__)

SOURCE = "ufc.com"
EVENTS_URL = "https://www.ufc.com/events"
HOME_URL = "https://www.ufc.com/"

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


@dataclass
class ParsedBout:
    card_segment: str | None
    bout_order: int
    weight_class: str | None
    scheduled_rounds: int | None
    red_name: str
    blue_name: str
    fmid: str


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


def _build_event_name(soup: BeautifulSoup, headliner: str | None) -> str | None:
    title = _meta_content(soup, "og:title") or ""
    series = title.replace("| UFC", "").strip(" |") or "UFC"
    if not headliner:
        return series
    headliner_vs = re.sub(r"\bvs\b(?!\.)", "vs.", headliner)
    return f"{series}: {headliner_vs}"


def _derive_broadcast(name: str | None) -> str | None:
    """ufc.com exposes no reliable per-event broadcaster in static HTML, so apply UFC's
    standard model: numbered events ("UFC 329") are pay-per-view; everything else
    (Fight Night, UFC on ESPN/ABC) streams on ESPN+/Fight Pass."""
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
            )
        )
    return bouts


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
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, source_id FROM events WHERE source = %s AND status = 'upcoming'",
            (SOURCE,),
        )
        rows = cursor.fetchall()
        stale_ids = [int(r[0]) for r in rows if str(r[1]) not in current_source_ids]
        for event_id in stale_ids:
            cursor.execute("UPDATE events SET status = 'completed' WHERE id = %s", (event_id,))
        return len(stale_ids)


def scrape_upcoming_events(dry_run: bool = False) -> Counter:
    settings = get_settings()
    counts: Counter = Counter()
    session = _new_session()
    session.get(HOME_URL, timeout=settings.request_timeout_seconds)

    listing = _get_soup(session, EVENTS_URL, settings)
    events = _parse_listing(listing)
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
                )
                event_id = upsert_event_meta(connection, record)
                delete_upcoming_fights(connection, event_id, SOURCE)
                for bout in event.bouts:
                    upsert_upcoming_fight(
                        connection,
                        UpcomingFightRecord(
                            event_id=event_id,
                            fighter_red_id=match(bout.red_name),
                            fighter_blue_id=match(bout.blue_name),
                            fighter_red_name=bout.red_name,
                            fighter_blue_name=bout.blue_name,
                            weight_class=bout.weight_class,
                            scheduled_rounds=bout.scheduled_rounds,
                            bout_order=bout.bout_order,
                            card_segment=bout.card_segment,
                            source=SOURCE,
                            source_id=bout.fmid,
                        ),
                    )
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
        "events_found", "details_fetched", "detail_errors", "bouts_parsed",
        "fighters_in_db", "bouts_red_matched", "bouts_blue_matched",
        "stale_completed", "events_written", "bouts_written", "write_errors",
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
