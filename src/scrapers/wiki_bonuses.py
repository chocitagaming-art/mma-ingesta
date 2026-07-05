"""Scrape UFC post-event bonus awards (FOTN / POTN) from English Wikipedia.

ufc.com never publishes the $50,000 bonuses in scrapeable static HTML, but every
UFC event has an English-Wikipedia article whose "Bonus awards" section lists
them the night of the event:

    Fight of the Night: Merab Dvalishvili vs. Petr Yan
    Performance of the Night: Bo Nickal and Carlos Prates

Pipeline per completed event (last --lookback-days, default 30, skipping events
that already have bonuses; or a single --event-id):

  1. TITLE — resolve the article title from events.name via the MediaWiki API:
     exact lookup first (action=query&redirects=1 follows redirects), then the
     name trimmed of its subtitle ("UFC 328: X vs Y" -> "UFC 328"), and finally
     list=search validated with the shared significant-token logic from
     repositories/events.py (so "UFC 327: Other vs Guy" can never be accepted
     for UFC 328).
  2. PARSE — fetch the rendered article (action=parse) and read the "Bonus
     awards" heading + its first <ul>. Each bullet is "<label>: <names>";
     footnote refs are stripped.
  3. MATCH — award names are matched against the event's own fights (corner
     names, falling back to the linked fighters' names for historical rows)
     with matching.fuzzy_match at IDENTITY_THRESHOLD (0.92): a bonus links DB
     ids, so a false positive would weld the award onto the wrong fight/fighter.
       - "Fight of the Night: A vs. B" -> the event fight whose two corners are
         A and B (either order) -> one fight_bonuses row (event, 'FOTN', fight).
       - "Performance of the Night: X and Y" -> one row per fighter
         (event, 'POTN', fighter_id), resolved from the corner he/she occupies.
  4. WRITE — INSERT ... ON CONFLICT DO NOTHING (unique partial indexes from
     migration 010), so re-runs and overlapping lookbacks are idempotent.

REQUIRES migration db/migrations/010_fase4.sql applied first.

Usage:
    python -m src.scrapers.wiki_bonuses --dry-run              # preview, no writes
    python -m src.scrapers.wiki_bonuses                        # last 30 days
    python -m src.scrapers.wiki_bonuses --lookback-days 365    # backfill wider
    python -m src.scrapers.wiki_bonuses --event-id 1044        # one event (idempotent)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .matching import IDENTITY_THRESHOLD, fuzzy_match
from .repositories.events import _significant_tokens

LOGGER = logging.getLogger(__name__)

API_URL = "https://en.wikipedia.org/w/api.php"
DEFAULT_LOOKBACK_DAYS = 30

FOTN = "FOTN"
POTN = "POTN"

_BONUS_LABELS = {
    "fight of the night": FOTN,
    "performance of the night": POTN,
}

# Footnote refs Wikipedia renders inside list items ("[a]", "[12]").
_FOOTNOTE_RE = re.compile(r"\[[^\]]*\]")
_VERSUS_RE = re.compile(r"\bvs\.?\b", re.IGNORECASE)
# POTN separators: commas, "and", ampersand. " and " requires surrounding
# whitespace so names containing the letters (e.g. "Fernanda") never split.
_CONJUNCTION_RE = re.compile(r",|\s+and\s+|\s*&\s*", re.IGNORECASE)

# fetch_json(params) -> decoded MediaWiki API response. Injected in tests.
FetchJson = Callable[[dict], dict]


@dataclass(frozen=True)
class ParsedBonus:
    bonus_type: str  # FOTN | POTN
    names: tuple[str, ...]  # FOTN: the two fighters; POTN: one fighter


@dataclass(frozen=True)
class EventFight:
    fight_id: int
    red_id: int | None
    blue_id: int | None
    red_name: str | None
    blue_name: str | None


# ----------------------------------------------------------- title resolution


def _query_exact_title(fetch_json: FetchJson, title: str) -> str | None:
    """The article title for an exact lookup (redirects followed), or None."""
    payload = fetch_json(
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
            "titles": title,
        }
    )
    pages = (payload.get("query") or {}).get("pages") or []
    for page in pages:
        if not page.get("missing") and page.get("title"):
            return str(page["title"])
    return None


def _title_tokens_match(event_name: str, candidate: str) -> bool:
    """Token guard for search results, reusing repositories/events.py's
    significant-token logic (stopwords like 'ufc/fight/night' dropped): accept
    a candidate only when its distinctive tokens are a subset of the event's
    ("UFC 328" for "UFC 328: Chimaev vs Strickland") or the overlap carries at
    least two distinctive tokens (typically both headliner surnames). A
    disjoint or contradictory title ("UFC 327: ...") is always rejected."""
    target = _significant_tokens(event_name)
    got = _significant_tokens(candidate)
    if not target or not got:
        return False
    overlap = target & got
    if not overlap:
        return False
    smaller, larger = (got, target) if len(got) <= len(target) else (target, got)
    return smaller <= larger or len(overlap) >= 2


def _search_title(fetch_json: FetchJson, event_name: str) -> str | None:
    payload = fetch_json(
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "list": "search",
            "srsearch": event_name,
            "srlimit": "5",
        }
    )
    results = ((payload.get("query") or {}).get("search")) or []
    for result in results:
        title = str(result.get("title") or "")
        if title and _title_tokens_match(event_name, title):
            return title
    return None


def resolve_wiki_title(fetch_json: FetchJson, event_name: str) -> str | None:
    """English-Wikipedia article title for a DB event name, or None.

    exact -> exact without the ':' subtitle -> token-validated search.
    """
    title = _query_exact_title(fetch_json, event_name)
    if title:
        return title
    trimmed = event_name.split(":", 1)[0].strip()
    if trimmed and trimmed != event_name:
        title = _query_exact_title(fetch_json, trimmed)
        if title:
            return title
    return _search_title(fetch_json, event_name)


def fetch_page_html(fetch_json: FetchJson, title: str) -> str | None:
    """Rendered HTML of the article (action=parse), or None."""
    payload = fetch_json(
        {
            "action": "parse",
            "format": "json",
            "formatversion": "2",
            "page": title,
            "prop": "text",
        }
    )
    text = (payload.get("parse") or {}).get("text")
    if isinstance(text, dict):  # formatversion=1 shape: {"*": "<html>"}
        text = text.get("*")
    return text or None


# ------------------------------------------------------------------- parsing


def _split_versus(value: str) -> list[str]:
    return [part.strip(" .") for part in _VERSUS_RE.split(value) if part.strip(" .")]


def _split_conjunction(value: str) -> list[str]:
    return [part.strip(" .") for part in _CONJUNCTION_RE.split(value) if part.strip(" .")]


def parse_bonus_awards(html: str) -> list[ParsedBonus]:
    """Bonuses from the article's "Bonus awards" section (h2 + first <ul>).

    Empty list when the article has no such section (pre-2014 events, cancelled
    cards) or the list yields nothing recognizable.
    """
    soup = BeautifulSoup(html, "lxml")
    heading = None
    for candidate in soup.find_all(["h2", "h3"]):
        if candidate.get_text(" ", strip=True).lower().startswith("bonus awards"):
            heading = candidate
            break
    if heading is None:
        return []
    bullet_list = heading.find_next("ul")
    if bullet_list is None:
        return []

    bonuses: list[ParsedBonus] = []
    for item in bullet_list.find_all("li", recursive=False):
        text = _FOOTNOTE_RE.sub("", item.get_text(" ", strip=True))
        label, _, value = text.partition(":")
        bonus_type = _BONUS_LABELS.get(" ".join(label.split()).lower())
        if bonus_type is None or not value.strip():
            continue
        if bonus_type == FOTN:
            names = _split_versus(value)
            if len(names) == 2:
                bonuses.append(ParsedBonus(FOTN, tuple(names)))
            else:
                LOGGER.warning("Unparseable FOTN line: %r", text)
        else:
            for name in _split_conjunction(value):
                bonuses.append(ParsedBonus(POTN, (name,)))
    return bonuses


# ------------------------------------------------------------------ matching


def _names_equal(scraped: str, stored: str | None) -> bool:
    # IDENTITY_THRESHOLD: the match attaches a DB id, so it keeps the strict
    # 0.92 cutoff every other fighter_id-linking path uses (see matching.py).
    return bool(stored) and fuzzy_match(scraped, stored, IDENTITY_THRESHOLD)


def match_fotn_fight(fights: list[EventFight], name_a: str, name_b: str) -> int | None:
    """The event fight whose two corners are name_a and name_b (either order)."""
    for fight in fights:
        if (_names_equal(name_a, fight.red_name) and _names_equal(name_b, fight.blue_name)) or (
            _names_equal(name_a, fight.blue_name) and _names_equal(name_b, fight.red_name)
        ):
            return fight.fight_id
    return None


def match_potn_fighter(fights: list[EventFight], name: str) -> int | None:
    """The fighter_id of the event participant carrying this name, or None
    (unlinked corner or no match — never guessed outside the event)."""
    for fight in fights:
        if fight.red_id is not None and _names_equal(name, fight.red_name):
            return fight.red_id
        if fight.blue_id is not None and _names_equal(name, fight.blue_name):
            return fight.blue_id
    return None


# ------------------------------------------------------------------------ db


def _get_target_events(connection, lookback_days: int) -> list[tuple[int, str]]:
    """(id, name) of completed events in the window with no bonuses yet."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT e.id, e.name
            FROM events e
            WHERE e.status = 'completed'
              AND e.event_date IS NOT NULL
              AND e.event_date >= CURRENT_DATE - %s
              AND NOT EXISTS (
                  SELECT 1 FROM fight_bonuses b WHERE b.event_id = e.id
              )
            ORDER BY e.event_date DESC, e.id DESC
            """,
            (lookback_days,),
        )
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def _get_event_by_id(connection, event_id: int) -> tuple[int, str] | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, name FROM events WHERE id = %s", (event_id,))
        row = cursor.fetchone()
        return (int(row[0]), str(row[1])) if row else None


def _get_event_fights(connection, event_id: int) -> list[EventFight]:
    """The event's fights with corner names: the scraped *_name columns when
    present (upcoming-sourced rows), else the linked fighters' names
    (historical ufcstats rows, where *_name is NULL)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT fi.id, fi.fighter_red_id, fi.fighter_blue_id,
                   COALESCE(fi.fighter_red_name, fr.name),
                   COALESCE(fi.fighter_blue_name, fb.name)
            FROM fights fi
            LEFT JOIN fighters fr ON fr.id = fi.fighter_red_id
            LEFT JOIN fighters fb ON fb.id = fi.fighter_blue_id
            WHERE fi.event_id = %s
            ORDER BY fi.id
            """,
            (event_id,),
        )
        return [
            EventFight(
                fight_id=int(row[0]),
                red_id=int(row[1]) if row[1] is not None else None,
                blue_id=int(row[2]) if row[2] is not None else None,
                red_name=row[3],
                blue_name=row[4],
            )
            for row in cursor.fetchall()
        ]


def insert_bonus(
    connection,
    event_id: int,
    bonus_type: str,
    *,
    fight_id: int | None = None,
    fighter_id: int | None = None,
) -> bool:
    """Idempotent insert: ON CONFLICT DO NOTHING fires against the partial
    unique indexes of migration 010, so re-runs never duplicate a bonus.
    Returns True when a row was actually inserted."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO fight_bonuses (event_id, bonus_type, fight_id, fighter_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (event_id, bonus_type, fight_id, fighter_id),
        )
        return cursor.rowcount > 0


# ----------------------------------------------------------------- pipeline


def _process_event(
    connection,
    fetch_json: FetchJson,
    counts: Counter,
    event_id: int,
    event_name: str,
    *,
    dry_run: bool,
) -> None:
    title = resolve_wiki_title(fetch_json, event_name)
    if title is None:
        counts["title_unresolved"] += 1
        LOGGER.info("No Wikipedia title resolved for event %d (%s)", event_id, event_name)
        return
    html = fetch_page_html(fetch_json, title)
    if not html:
        counts["page_errors"] += 1
        LOGGER.warning("Empty/unfetchable article %r for event %d", title, event_id)
        return
    bonuses = parse_bonus_awards(html)
    if not bonuses:
        # Expected for events without a section yet (article lags a few hours)
        # and for promos that paid no bonuses: nothing is written.
        counts["no_bonus_section"] += 1
        LOGGER.info("No 'Bonus awards' section in %r (event %d)", title, event_id)
        return

    counts["events_with_bonuses"] += 1
    fights = _get_event_fights(connection, event_id)
    for bonus in bonuses:
        counts["bonuses_parsed"] += 1
        if bonus.bonus_type == FOTN:
            fight_id = match_fotn_fight(fights, *bonus.names)
            if fight_id is None:
                counts["bonuses_unmatched"] += 1
                LOGGER.warning(
                    "FOTN %s not matched to a fight of event %d", " vs ".join(bonus.names), event_id
                )
                continue
            if not dry_run and insert_bonus(connection, event_id, FOTN, fight_id=fight_id):
                counts["bonuses_inserted"] += 1
        else:
            fighter_id = match_potn_fighter(fights, bonus.names[0])
            if fighter_id is None:
                counts["bonuses_unmatched"] += 1
                LOGGER.warning("POTN %r not matched on event %d", bonus.names[0], event_id)
                continue
            if not dry_run and insert_bonus(connection, event_id, POTN, fighter_id=fighter_id):
                counts["bonuses_inserted"] += 1


def backfill(
    connection,
    fetch_json: FetchJson,
    *,
    event_id: int | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    dry_run: bool = False,
) -> Counter:
    counts: Counter = Counter()
    if event_id is not None:
        target = _get_event_by_id(connection, event_id)
        targets = [target] if target else []
        if not target:
            LOGGER.warning("Event %d not found", event_id)
    else:
        targets = _get_target_events(connection, lookback_days)
    counts["events_targeted"] = len(targets)
    LOGGER.info("Completed events to check for bonuses: %d", len(targets))

    for target_id, name in targets:
        try:
            _process_event(connection, fetch_json, counts, target_id, name, dry_run=dry_run)
            if not dry_run:
                connection.commit()
        except Exception:
            connection.rollback()
            counts["event_errors"] += 1
            LOGGER.exception("Failed to process bonuses for event %d (%s)", target_id, name)

    if dry_run:
        # Release the read-only snapshot; guarantees dry-run never commits.
        connection.rollback()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape FOTN/POTN bonus awards from Wikipedia into fight_bonuses."
    )
    parser.add_argument("--event-id", type=int, default=None, help="Process only this event id.")
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help="Completed events within this window without bonuses yet (default %(default)s).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse + match but do not write.")
    args = parser.parse_args()
    configure_logging()

    settings = get_settings()
    session = requests.Session()
    session.headers.update({"User-Agent": settings.user_agent})

    def fetch_json(params: dict) -> dict:
        time.sleep(settings.request_delay_seconds)
        response = session.get(API_URL, params=params, timeout=settings.request_timeout_seconds)
        response.raise_for_status()
        return response.json()

    with connect(settings.database_url) as connection:
        counts = backfill(
            connection,
            fetch_json,
            event_id=args.event_id,
            lookback_days=args.lookback_days,
            dry_run=args.dry_run,
        )

    keys = [
        "events_targeted", "title_unresolved", "page_errors", "no_bonus_section",
        "events_with_bonuses", "bonuses_parsed", "bonuses_inserted",
        "bonuses_unmatched", "event_errors",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
