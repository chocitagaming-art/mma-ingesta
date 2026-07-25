"""Scrape official weigh-in results from ufc.com news articles into weigh_ins.

SOURCE (verified 2026-07): ufc.com publishes each event's weigh-in results the
day before the card as a news article. There is NO weigh-in block on the event
page itself (checked a completed event page: zero weigh-in markup), and the
article slug is NOT derivable from the event slug — real examples:

    /news/official-weigh-in-results-fight-night-fiziev-torres-baku
    /news/official-weigh-results-kape-horiguchi-vegas-119
    /news/official-weigh-results-ufc-freedom-250

The article is therefore DISCOVERED through ufc.com's server-rendered search
(https://www.ufc.com/search?query=...), which returns plain anchors to /news/
articles; a candidate is accepted only when its slug carries "weigh" AND shares
>=2 significant tokens with the event name (same guard as wiki_bonuses, so a
"UFC 327" article can never be welded onto UFC 328). The article body lists the
bouts in <h4> lines inside .field--name-text blocks:

    <h3><strong>MAIN CARD</strong></h3>
    <h4><strong>Co-Main Event -</strong> Middleweight Bout:
        Shara Magomedov (186) vs Michel Pereira (185.5)</h4>

Each line is "<weight class> Bout: Red (lbs) vs Blue (lbs)"; a trailing "*"
after a weight marks a missed weight. Promo links/paragraphs in between simply
don't match the pattern and are skipped.

LIMITATION: the article lands a few hours after the ceremony (Friday around
noon ET) and its publication is editorial — a card without an article (rare,
international fight weeks aside) yields no rows. mmajunkie/tapology were
evaluated as fallbacks and discarded (aggressive bot blocking).

Pipeline per target event (events dated within --lookback-days, default 7,
INCLUDING tomorrow's card — the article precedes the event; or --event-id):
  1. SEARCH  — "official weigh-in results <event tokens>" -> article URL.
  2. PARSE   — bout lines -> (red, red_lbs, red_missed, blue, blue_lbs, ...).
  3. MATCH   — each parsed bout against the event's own fights (corner names,
     falling back to the linked fighters' names) with matching.fuzzy_match at
     IDENTITY_THRESHOLD (0.92): a weigh-in links fight_id+fighter_id, so a
     false positive would weld the weight onto the wrong athlete.
  4. WRITE   — INSERT ... ON CONFLICT (fight_id, fighter_id) DO UPDATE with
     COALESCE (never overwrite a stored weight with NULL); re-runs idempotent.

REQUIRES migration db/migrations/011_fase5.sql applied first.

Usage:
    python -m src.scrapers.weigh_ins --dry-run             # preview, no writes
    python -m src.scrapers.weigh_ins                       # last 7 days + next card
    python -m src.scrapers.weigh_ins --lookback-days 30    # backfill wider
    python -m src.scrapers.weigh_ins --event-id 1044       # one event (idempotent)
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
from .rankings import BROWSER_HEADERS
from .repositories.events import _significant_tokens

LOGGER = logging.getLogger(__name__)

SEARCH_URL = "https://www.ufc.com/search"
BASE_URL = "https://www.ufc.com"
DEFAULT_LOOKBACK_DAYS = 7
# The article precedes the event (weigh-ins are the day BEFORE), so the target
# window extends this many days into the future to cover tomorrow's card.
LOOKAHEAD_DAYS = 2

# "Middleweight Bout: Shara Magomedov (186) vs Michel Pereira (185.5)*"
# The optional "<prefix> -" swallows "Main Event -" / "Co-Main Event -".
_BOUT_LINE_RE = re.compile(
    r"""^(?:.*?-\s*)?                     # "Main Event -" etc. (optional)
        [^:]*\bBout\s*:\s*                # "<weight class> Bout:"
        (?P<red>.+?)\s*
        \(\s*(?P<red_lbs>\d{2,3}(?:\.\d+)?)\s*\)\s*(?P<red_miss>\*?)\s*
        vs\.?\s*
        (?P<blue>.+?)\s*
        \(\s*(?P<blue_lbs>\d{2,3}(?:\.\d+)?)\s*\)\s*(?P<blue_miss>\*?)\s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

# fetch_html(url, params) -> page HTML. Injected in tests.
FetchHtml = Callable[[str, dict | None], str]


@dataclass(frozen=True)
class ParsedWeighIn:
    red_name: str
    red_lbs: float
    red_missed: bool
    blue_name: str
    blue_lbs: float
    blue_missed: bool


@dataclass(frozen=True)
class EventFight:
    fight_id: int
    red_id: int | None
    blue_id: int | None
    red_name: str | None
    blue_name: str | None


# ---------------------------------------------------------- article discovery


def _slug_tokens(href: str) -> set[str]:
    slug = href.rstrip("/").rsplit("/", 1)[-1]
    return set(slug.replace("-", " ").split())


def _search_for_weigh_in_article(fetch_html: FetchHtml, query: str, event_name: str) -> str | None:
    """First /news/ anchor of this search that passes the token guard."""
    html = fetch_html(SEARCH_URL, {"query": query})
    soup = BeautifulSoup(html, "lxml")
    target = _significant_tokens(event_name)
    for link in soup.select("a[href*='/news/']"):
        href = link.get("href", "")
        if "weigh" not in href.lower():
            continue
        overlap = target & _slug_tokens(href.lower())
        if len(overlap) >= 2:
            return href if href.startswith("http") else f"{BASE_URL}{href}"
        LOGGER.debug("Rejected weigh-in candidate %r for %r (overlap=%s)", href, event_name, overlap)
    return None


def find_weigh_in_article(fetch_html: FetchHtml, event_name: str) -> str | None:
    """Absolute URL of the event's weigh-in results article, or None.

    ufc.com's /search is server-rendered (Solr behind Drupal): the result list
    contains plain <a href="/news/..."> anchors. A candidate must carry "weigh"
    in its slug and share >=2 significant tokens (headliner surnames / card
    number, after dropping ufc/fight/night/... stopwords) with the event name.

    TWO queries, because the search is an AND over the article title and the two
    sites do not always name the same card alike: on 2026-07-25 our events row
    said "UFC Fight Night: Ankalaev vs. Guskov" while ufc.com titled it "UFC Abu
    Dhabi: Ankalaev vs Guskov", so the name-qualified query returned "No
    results" and the cron reported success having written nothing. The
    unqualified retry lists the most recent weigh-in articles instead, which is
    exactly the pool a Friday cron needs for Saturday's card.

    The retry widens WHICH articles are considered, never the acceptance rule:
    the same >=2-shared-token guard decides, so a neighbouring card's weights
    still cannot be welded onto this event.
    """
    qualified = _search_for_weigh_in_article(
        fetch_html, f"official weigh-in results {event_name}", event_name
    )
    if qualified is not None:
        return qualified
    LOGGER.info("No weigh-in article for %r by name; retrying unqualified search", event_name)
    return _search_for_weigh_in_article(fetch_html, "official weigh-in results", event_name)


# ------------------------------------------------------------------- parsing


def parse_weigh_ins(html: str) -> list[ParsedWeighIn]:
    """Bout weigh-in lines from a ufc.com weigh-in results article.

    Reads every text line of the article's .field--name-text body blocks (the
    bout lines are <h4>s, but the selector tolerates <p> variants) and keeps
    the ones matching "<class> Bout: A (lbs) vs B (lbs)". Interleaved promo
    links ("Preview The Entire ... Card Here") simply don't match.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[ParsedWeighIn] = []
    for block in soup.select(".field--name-text"):
        for element in block.find_all(["h4", "p", "li"]):
            text = element.get_text(" ", strip=True).replace("\xa0", " ")
            match = _BOUT_LINE_RE.match(text)
            if not match:
                continue
            results.append(
                ParsedWeighIn(
                    red_name=match.group("red").strip(),
                    red_lbs=float(match.group("red_lbs")),
                    red_missed=bool(match.group("red_miss")),
                    blue_name=match.group("blue").strip(),
                    blue_lbs=float(match.group("blue_lbs")),
                    blue_missed=bool(match.group("blue_miss")),
                )
            )
    return results


# ------------------------------------------------------------------ matching


def _names_equal(scraped: str, stored: str | None) -> bool:
    # IDENTITY_THRESHOLD: the match attaches DB ids, so it keeps the strict
    # 0.92 cutoff every other fighter_id-linking path uses (see matching.py).
    return bool(stored) and fuzzy_match(scraped, stored, IDENTITY_THRESHOLD)


def match_bout(fights: list[EventFight], entry: ParsedWeighIn) -> tuple[EventFight, bool] | None:
    """(fight, swapped) whose corners are the entry's two names, or None.

    swapped=True means the article's red is the DB's blue corner (the article
    order follows the announced card, which can flip after a reshuffle).
    """
    for fight in fights:
        if _names_equal(entry.red_name, fight.red_name) and _names_equal(entry.blue_name, fight.blue_name):
            return fight, False
        if _names_equal(entry.red_name, fight.blue_name) and _names_equal(entry.blue_name, fight.red_name):
            return fight, True
    return None


# ------------------------------------------------------------------------ db


def _get_target_events(connection, lookback_days: int) -> list[tuple[int, str]]:
    """(id, name) of events in the window (past lookback + the next card)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT e.id, e.name
            FROM events e
            WHERE e.event_date IS NOT NULL
              AND e.event_date >= CURRENT_DATE - %s
              AND e.event_date <= CURRENT_DATE + %s
              AND EXISTS (SELECT 1 FROM fights f WHERE f.event_id = e.id)
            ORDER BY e.event_date DESC, e.id DESC
            """,
            (lookback_days, LOOKAHEAD_DAYS),
        )
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def _get_event_by_id(connection, event_id: int) -> tuple[int, str] | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, name FROM events WHERE id = %s", (event_id,))
        row = cursor.fetchone()
        return (int(row[0]), str(row[1])) if row else None


def _get_event_fights(connection, event_id: int) -> list[EventFight]:
    """The event's fights with corner names: the scraped *_name columns when
    present (upcoming-sourced rows), else the linked fighters' names."""
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
              AND fi.status IS DISTINCT FROM 'cancelled'
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


def upsert_weigh_in(
    connection,
    fight_id: int,
    fighter_id: int,
    weight_lbs: float | None,
    missed_weight: bool | None,
) -> bool:
    """Idempotent upsert; the conflict branch never overwrites with NULL
    (COALESCE), so a later re-run with a worse parse keeps the stored data.
    Returns True when a row was inserted or updated."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO weigh_ins (fight_id, fighter_id, weight_lbs, missed_weight)
            VALUES (%s, %s, %s, COALESCE(%s, FALSE))
            ON CONFLICT (fight_id, fighter_id)
            DO UPDATE SET
                weight_lbs = COALESCE(EXCLUDED.weight_lbs, weigh_ins.weight_lbs),
                -- Raw argument re-bound (not EXCLUDED: that already went
                -- through the insert-side COALESCE) so NULL = "keep".
                missed_weight = COALESCE(%s, weigh_ins.missed_weight)
            """,
            (fight_id, fighter_id, weight_lbs, missed_weight, missed_weight),
        )
        return cursor.rowcount > 0


# ----------------------------------------------------------------- pipeline


def _process_event(
    connection,
    fetch_html: FetchHtml,
    counts: Counter,
    event_id: int,
    event_name: str,
    *,
    dry_run: bool,
) -> None:
    article_url = find_weigh_in_article(fetch_html, event_name)
    if article_url is None:
        counts["article_not_found"] += 1
        LOGGER.info("No weigh-in article found for event %d (%s)", event_id, event_name)
        return
    entries = parse_weigh_ins(fetch_html(article_url, None))
    if not entries:
        counts["article_unparseable"] += 1
        LOGGER.warning("Weigh-in article %r yielded no bouts (event %d)", article_url, event_id)
        return

    counts["events_with_article"] += 1
    fights = _get_event_fights(connection, event_id)
    for entry in entries:
        counts["bouts_parsed"] += 1
        matched = match_bout(fights, entry)
        if matched is None:
            counts["bouts_unmatched"] += 1
            LOGGER.warning(
                "Weigh-in bout %s vs %s not matched on event %d",
                entry.red_name, entry.blue_name, event_id,
            )
            continue
        fight, swapped = matched
        red = (entry.blue_lbs, entry.blue_missed) if swapped else (entry.red_lbs, entry.red_missed)
        blue = (entry.red_lbs, entry.red_missed) if swapped else (entry.blue_lbs, entry.blue_missed)
        for fighter_id, (weight_lbs, missed) in ((fight.red_id, red), (fight.blue_id, blue)):
            if fighter_id is None:
                counts["corners_unlinked"] += 1
                continue
            if not dry_run and upsert_weigh_in(connection, fight.fight_id, fighter_id, weight_lbs, missed):
                counts["weigh_ins_written"] += 1


def backfill(
    connection,
    fetch_html: FetchHtml,
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
    LOGGER.info("Events to check for weigh-ins: %d", len(targets))

    for target_id, name in targets:
        try:
            _process_event(connection, fetch_html, counts, target_id, name, dry_run=dry_run)
            if not dry_run:
                connection.commit()
        except Exception:
            connection.rollback()
            counts["event_errors"] += 1
            LOGGER.exception("Failed to process weigh-ins for event %d (%s)", target_id, name)

    if dry_run:
        # Release the read-only snapshot; guarantees dry-run never commits.
        connection.rollback()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape official weigh-in results from ufc.com news into weigh_ins."
    )
    parser.add_argument("--event-id", type=int, default=None, help="Process only this event id.")
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help="Events dated within this many past days (default %(default)s); "
             "the window always includes the next upcoming card.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse + match but do not write.")
    args = parser.parse_args()
    configure_logging()

    settings = get_settings()
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    def fetch_html(url: str, params: dict | None) -> str:
        time.sleep(settings.request_delay_seconds)
        response = session.get(url, params=params, timeout=settings.request_timeout_seconds)
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text

    with connect(settings.database_url) as connection:
        counts = backfill(
            connection,
            fetch_html,
            event_id=args.event_id,
            lookback_days=args.lookback_days,
            dry_run=args.dry_run,
        )

    keys = [
        "events_targeted", "article_not_found", "article_unparseable",
        "events_with_article", "bouts_parsed", "bouts_unmatched",
        "corners_unlinked", "weigh_ins_written", "event_errors",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
