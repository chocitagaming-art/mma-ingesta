"""Backfill referee + judges' scorecards from ufcstats fight-details pages.

SOURCE (verified 2026-07): every completed ufcstats fight (fights.source_id =
'/fight-details/<hash>') has a detail page whose header block carries:

    <i class="b-fight-details__text-item">
      <i class="b-fight-details__label">Referee:</i>
      <span class="">Herb Dean</span>
    </i>

and, for decisions only, the "Details:" paragraph lists one text-item per
judge ("<span>Ben Cartlidge</span> 28 - 29."). For finishes the Details
paragraph is free text ("Punches to Head On Ground") with no judge items, so
only the referee is captured there.

Score orientation: the page's two person blocks (b-fight-details__person, each
linking /fighter-details/<hash>) appear in the same order as the scores
("first - second"). The first person is normally the DB's red corner (the
event importer assigns red to the first fighter link), but the mapping is
re-derived per fight from the persons' fighter source ids — falling back to
fuzzy names at IDENTITY_THRESHOLD (0.92), then to the positional default — so
a historically swapped row still gets its scores on the right corners.

Pipeline (completed ufcstats fights without referee, events within
--lookback-days, default 30, newest first; --limit caps the page fetches):
  1. FETCH  — fight-details page via UfcStatsClient (rate limit ~1.25s/req +
     anti-bot challenge solving built in).
  2. PARSE  — referee + scorecards + persons (see markup above).
  3. WRITE  — fights.referee = COALESCE(parsed, referee) (never overwrites a
     stored value, never writes NULL) and one fight_scorecards row per judge
     with INSERT ... ON CONFLICT DO NOTHING (unique (fight_id, judge_name)).

REQUIRES migration db/migrations/011_fase5.sql applied first.

Usage:
    python -m src.scrapers.fight_officials --dry-run           # preview
    python -m src.scrapers.fight_officials                     # last 30 days
    python -m src.scrapers.fight_officials --lookback-days 3650 --limit 200   # backfill batch
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .config import get_settings
from .db import connect
from .http import UfcStatsClient
from .logging_config import configure_logging
from .matching import IDENTITY_THRESHOLD, fuzzy_match
from .utils import source_id_from_url

LOGGER = logging.getLogger(__name__)

BASE_URL = "http://ufcstats.com"
DEFAULT_LOOKBACK_DAYS = 30

_SCORE_RE = re.compile(r"(\d{1,3})\s*-\s*(\d{1,3})")


@dataclass(frozen=True)
class ParsedScorecard:
    judge_name: str
    first_score: int   # score of the page's FIRST person block
    second_score: int  # score of the page's SECOND person block


@dataclass(frozen=True)
class ParsedOfficials:
    referee: str | None
    scorecards: list[ParsedScorecard]
    # (fighter source_id '/fighter-details/<hash>', display name) in page order.
    persons: list[tuple[str | None, str]]


@dataclass(frozen=True)
class TargetFight:
    fight_id: int
    source_id: str
    red_source_id: str | None
    blue_source_id: str | None
    red_name: str | None
    blue_name: str | None


# ------------------------------------------------------------------- parsing


def _label_value(item) -> str:
    """Text of a b-fight-details__text-item minus its __label child."""
    parts = [
        text for text in item.find_all(string=True, recursive=True)
        if not text.find_parent(class_="b-fight-details__label")
    ]
    return " ".join(" ".join(parts).split())


def parse_fight_officials(soup: BeautifulSoup) -> ParsedOfficials:
    referee: str | None = None
    scorecards: list[ParsedScorecard] = []
    for item in soup.select("i.b-fight-details__text-item"):
        label = item.select_one("i.b-fight-details__label")
        if label is None:
            continue
        if label.get_text(strip=True).rstrip(":").lower() == "referee":
            referee = _label_value(item) or None

    # Judge items live in the "Details:" paragraph and have NO label of their
    # own: a <span> with the judge's name followed by "28 - 29.".
    for item in soup.select("p.b-fight-details__text i.b-fight-details__text-item"):
        if item.select_one("i.b-fight-details__label") is not None:
            continue
        judge_el = item.select_one("span")
        if judge_el is None:
            continue
        judge_name = " ".join(judge_el.get_text(" ", strip=True).split())
        score_match = _SCORE_RE.search(_label_value(item).replace(judge_name, "", 1))
        if not judge_name or score_match is None:
            continue
        scorecards.append(
            ParsedScorecard(
                judge_name=judge_name,
                first_score=int(score_match.group(1)),
                second_score=int(score_match.group(2)),
            )
        )

    persons: list[tuple[str | None, str]] = []
    for person in soup.select(".b-fight-details__person"):
        link = person.select_one("a[href*='/fighter-details/']")
        if link is None:
            continue
        href = link.get("href", "")
        persons.append(
            (
                source_id_from_url(href) if href else None,
                " ".join(link.get_text(" ", strip=True).split()),
            )
        )
    return ParsedOfficials(referee=referee, scorecards=scorecards, persons=persons)


# ------------------------------------------------------------------ mapping


def first_person_is_red(officials: ParsedOfficials, fight: TargetFight) -> bool:
    """Whether the page's first person block is the DB fight's red corner.

    source_id match first (exact, both are '/fighter-details/<hash>'), then
    fuzzy names at IDENTITY_THRESHOLD; defaults to True (the importer assigns
    red to the first fighter link, so page order == corner order normally).
    """
    if len(officials.persons) < 2:
        return True
    first_source, first_name = officials.persons[0]
    if first_source:
        if fight.red_source_id and first_source == fight.red_source_id:
            return True
        if fight.blue_source_id and first_source == fight.blue_source_id:
            return False
    if fight.red_name and fuzzy_match(first_name, fight.red_name, IDENTITY_THRESHOLD):
        return True
    if fight.blue_name and fuzzy_match(first_name, fight.blue_name, IDENTITY_THRESHOLD):
        return False
    return True


# ------------------------------------------------------------------------ db


def list_target_fights(
    connection, lookback_days: int, limit: int | None
) -> list[TargetFight]:
    """Completed ufcstats fights still missing a referee, newest events first."""
    sql = """
        SELECT f.id, f.source_id,
               fr.source_id, fb.source_id,
               COALESCE(f.fighter_red_name, fr.name),
               COALESCE(f.fighter_blue_name, fb.name)
        FROM fights f
        JOIN events e ON e.id = f.event_id
        LEFT JOIN fighters fr ON fr.id = f.fighter_red_id
        LEFT JOIN fighters fb ON fb.id = f.fighter_blue_id
        WHERE f.source = 'ufcstats'
          AND f.source_id IS NOT NULL
          AND f.referee IS NULL
          AND (f.winner_id IS NOT NULL OR f.method IS NOT NULL)
          AND e.event_date IS NOT NULL
          AND e.event_date >= CURRENT_DATE - %s
        ORDER BY e.event_date DESC, f.id
    """
    params: list = [lookback_days]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return [
            TargetFight(
                fight_id=int(row[0]),
                source_id=str(row[1]),
                red_source_id=row[2],
                blue_source_id=row[3],
                red_name=row[4],
                blue_name=row[5],
            )
            for row in cursor.fetchall()
        ]


def update_fight_referee(connection, fight_id: int, referee: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            # COALESCE(referee, %s): the target list already filters
            # referee IS NULL, but a concurrent/older value is never stomped.
            "UPDATE fights SET referee = COALESCE(referee, %s), updated_at = NOW() WHERE id = %s",
            (referee, fight_id),
        )


def insert_scorecard(
    connection, fight_id: int, judge_name: str, red_score: int, blue_score: int
) -> bool:
    """Idempotent: ON CONFLICT DO NOTHING against UNIQUE (fight_id, judge_name).
    Returns True when a row was actually inserted."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO fight_scorecards (fight_id, judge_name, red_score, blue_score)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (fight_id, judge_name) DO NOTHING
            """,
            (fight_id, judge_name, red_score, blue_score),
        )
        return cursor.rowcount > 0


# ----------------------------------------------------------------- pipeline


def _fight_detail_url(source_id: str) -> str:
    if source_id.startswith("http"):
        return source_id
    if source_id.startswith("/"):
        return f"{BASE_URL}{source_id}"
    return f"{BASE_URL}/fight-details/{source_id}"


def _process_fight(
    connection, client: UfcStatsClient, counts: Counter, fight: TargetFight, *, dry_run: bool
) -> None:
    page = client.fetch(_fight_detail_url(fight.source_id))
    officials = parse_fight_officials(page.soup)
    if officials.referee is None and not officials.scorecards:
        counts["fights_without_officials"] += 1
        LOGGER.info("No referee/scorecards parsed for fight %d (%s)", fight.fight_id, fight.source_id)
        return

    if officials.referee is not None:
        counts["referees_parsed"] += 1
        if not dry_run:
            update_fight_referee(connection, fight.fight_id, officials.referee)

    red_first = first_person_is_red(officials, fight)
    for card in officials.scorecards:
        counts["scorecards_parsed"] += 1
        red_score, blue_score = (
            (card.first_score, card.second_score)
            if red_first
            else (card.second_score, card.first_score)
        )
        if not dry_run and insert_scorecard(
            connection, fight.fight_id, card.judge_name, red_score, blue_score
        ):
            counts["scorecards_inserted"] += 1


def backfill(
    connection,
    client: UfcStatsClient,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int | None = None,
    dry_run: bool = False,
) -> Counter:
    counts: Counter = Counter()
    fights = list_target_fights(connection, lookback_days, limit)
    counts["fights_targeted"] = len(fights)
    LOGGER.info("Completed fights missing referee: %d (dry_run=%s)", len(fights), dry_run)

    for fight in fights:
        try:
            _process_fight(connection, client, counts, fight, dry_run=dry_run)
            if not dry_run:
                connection.commit()
        except Exception:
            connection.rollback()
            counts["fight_errors"] += 1
            LOGGER.exception("Failed to process officials for fight %d (%s)", fight.fight_id, fight.source_id)

    if dry_run:
        # Release the read-only snapshot; guarantees dry-run never commits.
        connection.rollback()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill fights.referee + fight_scorecards from ufcstats fight-details."
    )
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help="Completed fights of events within this window (default %(default)s).",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max fights to fetch this run.")
    parser.add_argument("--dry-run", action="store_true", help="Parse but do not write.")
    args = parser.parse_args()
    configure_logging()

    settings = get_settings()
    client = UfcStatsClient(settings)
    with connect(settings.database_url) as connection:
        counts = backfill(
            connection,
            client,
            lookback_days=args.lookback_days,
            limit=args.limit,
            dry_run=args.dry_run,
        )

    keys = [
        "fights_targeted", "fights_without_officials", "referees_parsed",
        "scorecards_parsed", "scorecards_inserted", "fight_errors",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
