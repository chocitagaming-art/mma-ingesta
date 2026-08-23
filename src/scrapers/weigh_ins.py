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
    <h4><strong>Main Event -</strong> UFC Welterweight Championship:
        Islam Makhachev (170) vs Ian Machado Garry (170)</h4>
    <h4><strong>Co-Main Event -</strong> Middleweight Bout:
        Shara Magomedov (186) vs Michel Pereira (185.5)</h4>

Most lines are "<weight class> Bout: Red (lbs) vs Blue (lbs)", but TITLE FIGHTS
say "<division> Championship:" instead and never carry the word "Bout" — see
_BOUT_LINE_RE. A trailing "*" after a weight marks a missed weight. Promo
links/paragraphs in between simply don't match the pattern and are skipped;
a skipped line that still LOOKS like a bout is counted and logged rather than
dropped in silence (_BOUT_SHAPE_RE).

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
#
# 🪤 THE KEYWORD IS A LIST, NOT "Bout". Title fights are the one line ufc.com
# does NOT call a Bout: it writes "UFC Welterweight Championship: Islam
# Makhachev (170) vs Ian Machado Garry (170)". Requiring the literal "Bout"
# dropped both title lines of UFC 330 in silence, which is why on 2026-08-15
# not one of the 484 title fights in `fights` had ever had a weigh-in row.
# The list stays CLOSED on purpose: dropping the keyword altogether and
# accepting any "<anything>: A (n) vs B (n)" would let prose with two
# parenthesised numbers through, and while match_bout would refuse to weld it
# onto the wrong athlete, it could still write a wrong number onto the right
# pair.
_BOUT_LINE_RE = re.compile(
    r"""^(?:.*?-\s*)?                     # "Main Event -" etc. (optional)
        # "<class> Bout:" / "<div> Championship:" / "Catchweight Bout (130-lbs):"
        # The optional bracket is consumed BEFORE the colon on purpose: in a
        # catchweight line it holds the agreed limit, which is a third
        # parenthesised number that belongs to NOBODY. Leaving it to <red> would
        # let "130" be read as an athlete's weight.
        [^:]*\b(?:Bout|Championship|Title)(?:\s*\([^)]*\))?\s*:\s*
        (?P<red>.+?)\s*
        # The over-limit asterisk sits EITHER side of the closing bracket:
        # "(158)*" (5 of 24 articles) and "(157.5*)" (2 of 24) are both real.
        # One run of one or two: `*` and `**` are two different footnotes.
        \(\s*(?P<red_lbs>\d{2,3}(?:\.\d+)?)\s*(?P<red_miss_in>\*{1,2})?\s*\)\s*(?P<red_miss>\*{1,2})?\s*
        vs\.?\s*
        (?P<blue>.+?)\s*
        \(\s*(?P<blue_lbs>\d{2,3}(?:\.\d+)?)\s*(?P<blue_miss_in>\*{1,2})?\s*\)\s*(?P<blue_miss>\*{1,2})?\s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

# The tripwire for the NEXT time ufc.com changes the wording. Matches the SHAPE
# of a bout line — two parenthesised weights either side of a "vs" — without
# caring about the keyword, so a line that clearly announces a bout but fails
# _BOUT_LINE_RE gets counted and logged instead of vanishing. It deliberately
# needs BOTH weights: promo copy says "Makhachev vs Machado Garry" all the time
# and must never trip the alarm, because an alarm that cries wolf is an alarm
# nobody reads.
#
# 🪤 THE TRIPWIRE MUST BE LOOSER THAN WHAT IT WATCHES, AND IT WASN'T. Until
# 2026-08-15 this pattern carried its own copy of `\(\s*\d{2,3}(?:\.\d+)?\s*\)`
# — the very subpattern _BOUT_LINE_RE uses — so an asterisk INSIDE the bracket
# broke both at once and the loss was silent. Measured against the real Oklahoma
# City article: 12 bout lines, 10 parsed, and `counts` came back as the EMPTY
# dict, not even the key at zero.
#
#     Lightweight Bout: Chase Hooper (157.5*) vs Mitch Ramirez (155.5)
#     Featherweight Bout: Ezra Elliott (147.5**) vs Damien Anderson (146)
#
# The asterisks are now tolerated on BOTH sides of the closing bracket and in
# runs of one or two (ufc.com uses `*` and `**` as two separate footnotes on the
# same page). A tripwire that shares a blind spot with the thing it watches is
# not a tripwire: it is a second copy of the bug that also reports success.
_BOUT_SHAPE_RE = re.compile(
    r"\(\s*\d{2,3}(?:\.\d+)?\s*\*{0,2}\s*\)\s*\*{0,2}\s*"
    r"vs\.?\s.*?"
    r"\(\s*\d{2,3}(?:\.\d+)?\s*\*{0,2}\s*\)",
    re.IGNORECASE,
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


# Words that name the venue, not the town. Stripped when the city has to be
# recovered from the venue itself ("Belgrade Arena, BG, Serbia" -> "Belgrade").
_VENUE_WORDS = re.compile(
    r"\b(arena|center|centre|stadium|apex|hall|coliseum|park|dome|garden|forum|pavilion)\b",
    re.IGNORECASE,
)


def _event_city(location: str | None) -> str:
    """Best guess at the host city from an `events.location` string.

    The eight real shapes in the DB on 2026-08-23 put the city in the SECOND
    comma-part ("Golden 1 Center, Sacramento, CA, United States"). The one that
    breaks the rule is "Belgrade Arena, BG, Serbia", where the second part is a
    two-letter code and the venue name carries the city — hence the length
    guard and the venue-word strip.
    """
    parts = [part.strip() for part in (location or "").split(",") if part.strip()]
    if not parts:
        return ""
    if len(parts) >= 2 and len(parts[1]) > 3:
        return parts[1]
    return _VENUE_WORDS.sub("", parts[0]).strip()


def find_weigh_in_article(
    fetch_html: FetchHtml, event_name: str, location: str | None = None
) -> str | None:
    """Absolute URL of the event's weigh-in results article, or None.

    ufc.com's /search is server-rendered (Solr behind Drupal): the result list
    contains plain <a href="/news/..."> anchors. A candidate must carry "weigh"
    in its slug and share >=2 significant tokens (headliner surnames / card
    number, after dropping ufc/fight/night/... stopwords) with the event name.

    THREE queries, because the search is an AND over the article TITLE and the
    two sites do not always name the same card alike: on 2026-07-25 our events
    row said "UFC Fight Night: Ankalaev vs. Guskov" while ufc.com titled it
    "UFC Abu Dhabi", so the name-qualified query returned "No results" and the
    cron reported success having written nothing.

    1. BY NAME. The precise one when it works, and it is tried first.
    2. BY CITY, added 2026-08-23. ufc.com titles many cards by host city
       ("Official Weigh-In Results | UFC Sacramento"), so the city is the term
       its title actually contains. Measured that day: the name query fails on
       FIVE of the ten most recent cards (1086 Sacramento, 1063 Belgrade, 1062
       Abu Dhabi, 1061 Oklahoma City, 1059 Baku) and searching by city recovers
       all five.
    3. UNQUALIFIED. Lists the most recent weigh-in articles. It is a narrow net
       and getting narrower: measured 2026-08-23 it returns only ~4 anchors and
       `page` is ignored, so an article drops out of reach within days. That is
       why the city step exists rather than leaning on this one.

    Each step widens WHICH articles are considered, never the acceptance rule:
    the same >=2-shared-token guard decides, so a neighbouring card's weights
    still cannot be welded onto this event. That matters most for the city
    query, since a city hosts many cards over the years.
    """
    qualified = _search_for_weigh_in_article(
        fetch_html, f"official weigh-in results {event_name}", event_name
    )
    if qualified is not None:
        return qualified

    city = _event_city(location)
    if city:
        by_city = _search_for_weigh_in_article(
            fetch_html, f"official weigh-in results ufc {city}", event_name
        )
        if by_city is not None:
            LOGGER.info("Weigh-in article for %r found by city %r", event_name, city)
            return by_city

    LOGGER.info(
        "No weigh-in article for %r by name%s; retrying unqualified search",
        event_name,
        f" nor by city {city!r}" if city else "",
    )
    return _search_for_weigh_in_article(fetch_html, "official weigh-in results", event_name)


# ------------------------------------------------------------------- parsing


def _strip_name_marker(raw: str) -> tuple[str, bool]:
    """(name without a glued over-limit marker, whether one was there).

    `(?P<red>.+?)` is happy to swallow a leading asterisk, and nothing
    downstream objects: fold() (matching.py) does not remove it either, and
    fold_ratio("*Alan Jouban", "Alan Jouban") scores 0.9565 — above the 0.92
    identity cutoff — so the corner matches and the row is written with
    missed_weight=FALSE. That is worse than the lost lines of the tripwire bug:
    a lost line leaves no row, this one publishes the opposite of the record.

    Only the EDGES of the already-captured name are touched, never the line. The
    footnotes at the foot of the article start with the same asterisks and are
    the only independent oracle this scraper has; stripping them off the whole
    line to "clean it up" would destroy the one thing that can check the parser.
    """
    name = raw.strip()
    clean = name.strip("*").strip()
    return clean, clean != name


def parse_weigh_ins(html: str, counts: Counter | None = None) -> list[ParsedWeighIn]:
    """Bout weigh-in lines from a ufc.com weigh-in results article.

    Reads every text line of the article's .field--name-text body blocks (the
    bout lines are <h4>s, but the selector tolerates <p> variants) and keeps
    the ones matching "<class> Bout:" or "<division> Championship:" followed by
    "A (lbs) vs B (lbs)". Interleaved promo links ("Preview The Entire ... Card
    Here") simply don't match.

    `counts` is optional and opt-in so existing callers keep working. When it
    is passed, a line shaped like a bout that this parser could NOT read bumps
    `bout_shaped_lines_unparsed` and logs a warning. That counter is the whole
    lesson of the title-fight bug: the skip was silent, so the job finished
    green with "weigh_ins_written: 42, event_errors: 0" while losing four
    weights, and nobody found out until someone looked at the page.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[ParsedWeighIn] = []
    for block in soup.select(".field--name-text"):
        for element in block.find_all(["h4", "p", "li"]):
            text = element.get_text(" ", strip=True).replace("\xa0", " ")
            match = _BOUT_LINE_RE.match(text)
            if not match:
                if counts is not None and _BOUT_SHAPE_RE.search(text):
                    counts["bout_shaped_lines_unparsed"] += 1
                    LOGGER.warning(
                        "Weigh-in line looks like a bout but did not parse: %r", text
                    )
                continue
            red_name, red_marked = _strip_name_marker(match.group("red"))
            blue_name, blue_marked = _strip_name_marker(match.group("blue"))
            results.append(
                ParsedWeighIn(
                    red_name=red_name,
                    red_lbs=float(match.group("red_lbs")),
                    # Any of the three positions marks the same thing: over the
                    # limit. Dropping the one glued to the name would write
                    # missed_weight=FALSE, i.e. the opposite of the record.
                    red_missed=bool(
                        match.group("red_miss") or match.group("red_miss_in") or red_marked
                    ),
                    blue_name=blue_name,
                    blue_lbs=float(match.group("blue_lbs")),
                    blue_missed=bool(
                        match.group("blue_miss") or match.group("blue_miss_in") or blue_marked
                    ),
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


def _get_target_events(connection, lookback_days: int) -> list[tuple[int, str, str | None]]:
    """(id, name, location) of events in the window (past lookback + next card).

    `location` rides along for the by-city article search in
    `find_weigh_in_article`; it is nullable, and a missing one simply skips
    that step.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT e.id, e.name, e.location
            FROM events e
            WHERE e.event_date IS NOT NULL
              AND e.event_date >= CURRENT_DATE - %s
              AND e.event_date <= CURRENT_DATE + %s
              AND EXISTS (SELECT 1 FROM fights f WHERE f.event_id = e.id)
            ORDER BY e.event_date DESC, e.id DESC
            """,
            (lookback_days, LOOKAHEAD_DAYS),
        )
        return [
            (int(row[0]), str(row[1]), str(row[2]) if row[2] is not None else None)
            for row in cursor.fetchall()
        ]


def _get_event_by_id(connection, event_id: int) -> tuple[int, str, str | None] | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, name, location FROM events WHERE id = %s", (event_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return (int(row[0]), str(row[1]), str(row[2]) if row[2] is not None else None)


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
    """Idempotent upsert. Returns True when a row was inserted or updated.

    The COALESCE protects `weight_lbs` and ONLY `weight_lbs`: a later re-run
    with a worse parse keeps the stored weight.

    🪤 IT DOES NOT PROTECT `missed_weight`, however it reads. The value handed
    in always comes out of `bool(...)` in parse_weigh_ins, so it is never None,
    so `COALESCE(%s, weigh_ins.missed_weight)` is a no-op for that column and
    the cron rewrites the boolean on every pass. The column is NOT NULL DEFAULT
    FALSE (011_fase5.sql:40), so there is no "don't know" state to fall back on:
    the day ufc.com moves the asterisk somewhere the line still matches but the
    marker is not seen, every correctly flagged athlete gets silently unflagged.

    That is accepted on purpose, and written down rather than fixed: the cron is
    the authority for this column. Giving it a third state means a schema change
    plus a consumer that today treats null and false alike (event-weigh-ins.tsx:
    62 — and null being falsy, TypeScript would not say a word). The defence is
    on the other side: the footnote cross-check in test_weigh_ins.py, which
    reads a second source on the same page that the parser cannot fake."""
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
    location: str | None = None,
    *,
    dry_run: bool,
) -> None:
    article_url = find_weigh_in_article(fetch_html, event_name, location)
    if article_url is None:
        counts["article_not_found"] += 1
        LOGGER.info("No weigh-in article found for event %d (%s)", event_id, event_name)
        return
    entries = parse_weigh_ins(fetch_html(article_url, None), counts)
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

    for target_id, name, location in targets:
        try:
            _process_event(
                connection, fetch_html, counts, target_id, name, location, dry_run=dry_run
            )
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
        "events_with_article", "bouts_parsed", "bout_shaped_lines_unparsed",
        "bouts_unmatched", "corners_unlinked", "weigh_ins_written", "event_errors",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
