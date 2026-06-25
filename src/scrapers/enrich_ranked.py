"""Scoped ESPN enrichment for the *ranked* fighter subset.

Many ranked fighters are source='ufcstats' and were never matched to an ESPN
athlete during the bulk enrichment, so they have NULL headshot_url / nationality
and render as initials + no flag on the frontend. Reprocessing all ~2,800
fighters (a full `espn` run) is wasteful; instead this resolves each gap fighter
individually via ESPN's search API, fetches the athlete detail (reusing espn.py),
verifies the name matches, and fills ONLY the NULL columns
(update_fighter_enrichment uses COALESCE, so existing data is never overwritten).

Usage:
    python -m src.scrapers.enrich_ranked --dry-run     # report, no writes
    python -m src.scrapers.enrich_ranked               # enrich the ranked gap set
    python -m src.scrapers.enrich_ranked --import "Tyrell Fortune"   # import a missing fighter
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter

import requests

from .config import Settings, get_settings
from .db import connect
from .espn import EspnAthlete, _fetch_athlete
from .logging_config import configure_logging
from .matching import DEFAULT_THRESHOLD, fold as _fold, fold_ratio as _similar
from .repositories.fighters import (
    get_fighter_id_by_source,
    update_fighter_enrichment,
    upsert_fighter,
)
from .models import FighterRecord


LOGGER = logging.getLogger(__name__)

ESPN_SOURCE = "espn"
ESPN_SEARCH_URL = "https://site.web.api.espn.com/apis/search/v2"
# Athlete detail endpoints tried in order (league-scoped first, then mma-wide).
ESPN_ATHLETE_DETAIL_URLS = [
    "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/athletes/{id}",
    "https://sports.core.api.espn.com/v2/sports/mma/athletes/{id}",
]
UID_ID_PATTERN = re.compile(r"a:(\d+)")
# Enrichment (not DB identity linking): use the canonical compromise cutoff.
# See src/scrapers/matching.py for the threshold policy.
NAME_MATCH_THRESHOLD = DEFAULT_THRESHOLD
REQUEST_DELAY_SECONDS = 0.35


def _build_session(settings: Settings) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": settings.user_agent.replace("ufcstats.com", "espn.com"),
        }
    )
    return session


def _search_espn_athlete(session: requests.Session, name: str) -> tuple[str, str] | None:
    """Return (espn_athlete_id, espn_display_name) for the best MMA player match, or None."""
    response = session.get(ESPN_SEARCH_URL, params={"query": name, "limit": 10}, timeout=30)
    response.raise_for_status()
    data = response.json()
    best: tuple[float, str, str] | None = None
    for group in data.get("results", []):
        if group.get("type") != "player":
            continue
        for item in group.get("contents", []):
            if item.get("sport") != "mma":
                continue
            uid = str(item.get("uid") or "")
            id_match = UID_ID_PATTERN.search(uid)
            if not id_match:
                continue
            espn_name = str(item.get("displayName") or "").strip()
            if not espn_name:
                continue
            score = _similar(name, espn_name)
            if best is None or score > best[0]:
                best = (score, id_match.group(1), espn_name)
    if best is None or best[0] < NAME_MATCH_THRESHOLD:
        if best is not None:
            LOGGER.info("Rejected weak match for %r: %r (%.2f)", name, best[2], best[0])
        return None
    return best[1], best[2]


def _fetch_athlete_by_id(session: requests.Session, athlete_id: str) -> EspnAthlete | None:
    for template in ESPN_ATHLETE_DETAIL_URLS:
        try:
            return _fetch_athlete(session, template.format(id=athlete_id))
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                continue
            raise
    return None


def _resolve_athlete(session: requests.Session, name: str) -> EspnAthlete | None:
    found = _search_espn_athlete(session, name)
    if found is None:
        return None
    athlete_id, _espn_name = found
    athlete = _fetch_athlete_by_id(session, athlete_id)
    time.sleep(REQUEST_DELAY_SECONDS)
    if athlete is None:
        return None
    # Final safety: the detail name must still match the fighter we are enriching.
    if _similar(name, athlete.full_name) < NAME_MATCH_THRESHOLD:
        LOGGER.info("Detail name mismatch for %r: %r", name, athlete.full_name)
        return None
    return athlete


def _get_ranked_gap_fighters(connection) -> list[tuple[int, str, bool, bool]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT f.id, f.name,
                   f.headshot_url IS NULL AS no_photo,
                   f.nationality IS NULL AS no_nat
            FROM rankings r
            JOIN fighters f ON f.id = r.fighter_id
            WHERE r.snapshot_date = (SELECT MAX(snapshot_date) FROM rankings)
              AND (f.headshot_url IS NULL OR f.nationality IS NULL)
            ORDER BY f.name
            """
        )
        return [(int(r[0]), str(r[1]), bool(r[2]), bool(r[3])) for r in cursor.fetchall()]


def enrich_ranked_fighters(dry_run: bool = False) -> Counter:
    settings = get_settings()
    session = _build_session(settings)
    counts: Counter = Counter()
    with connect(settings.database_url) as connection:
        gaps = _get_ranked_gap_fighters(connection)
        counts["gap_fighters"] = len(gaps)
        for fighter_id, name, _no_photo, _no_nat in gaps:
            try:
                athlete = _resolve_athlete(session, name)
                time.sleep(REQUEST_DELAY_SECONDS)
            except Exception as exc:  # network / parsing
                counts["errors"] += 1
                LOGGER.warning("Lookup failed for %r: %s", name, exc)
                continue
            if athlete is None:
                counts["unresolved"] += 1
                LOGGER.info("No ESPN match for %r", name)
                continue
            counts["resolved"] += 1
            if athlete.headshot_url:
                counts["espn_has_photo"] += 1
            if athlete.nationality:
                counts["espn_has_nat"] += 1
            if dry_run:
                continue
            updated = update_fighter_enrichment(
                connection,
                fighter_id,
                nickname=athlete.nickname,
                headshot_url=athlete.headshot_url,
                nationality=athlete.nationality,
                birth_date=athlete.birth_date,
                height_cm=athlete.height_cm,
                reach_cm=athlete.reach_cm,
                weight_grams=athlete.weight_grams,
                stance=athlete.stance,
            )
            connection.commit()
            if updated:
                counts["updated"] += 1
    return counts


def import_fighter_by_name(name: str, dry_run: bool = False) -> int | None:
    """Import a single fighter from ESPN into the fighters table. Returns the fighter id."""
    settings = get_settings()
    session = _build_session(settings)
    athlete = _resolve_athlete(session, name)
    if athlete is None:
        LOGGER.warning("Could not resolve %r on ESPN; not imported.", name)
        return None
    LOGGER.info(
        "Resolved %r -> ESPN id %s (%s, nat=%s, photo=%s)",
        name, athlete.athlete_id, athlete.full_name, athlete.nationality,
        bool(athlete.headshot_url),
    )
    if dry_run:
        return None
    with connect(settings.database_url) as connection:
        existing = get_fighter_id_by_source(connection, ESPN_SOURCE, athlete.athlete_id)
        if existing is not None:
            LOGGER.info("%r already present (id=%s)", athlete.full_name, existing)
            return existing
        record = FighterRecord(
            name=athlete.full_name,
            nickname=athlete.nickname,
            headshot_url=athlete.headshot_url,
            nationality=athlete.nationality,
            birth_date=athlete.birth_date,
            height_cm=athlete.height_cm,
            reach_cm=athlete.reach_cm,
            stance=athlete.stance,
            weight_grams=athlete.weight_grams,
            wins=0,
            losses=0,
            draws=0,
            source=ESPN_SOURCE,
            source_id=athlete.athlete_id,
        )
        fighter_id = upsert_fighter(connection, record)
        connection.commit()
        LOGGER.info("Imported %r as fighter id %s", athlete.full_name, fighter_id)
        return fighter_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Scoped ESPN enrichment for ranked fighters.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve + report but do not write.")
    parser.add_argument(
        "--import",
        dest="import_names",
        action="append",
        default=[],
        metavar="NAME",
        help="Import a missing fighter by name from ESPN (repeatable).",
    )
    args = parser.parse_args()
    configure_logging()

    if args.import_names:
        for name in args.import_names:
            fighter_id = import_fighter_by_name(name, dry_run=args.dry_run)
            print(json.dumps({"import": name, "fighter_id": fighter_id}))
        return

    counts = enrich_ranked_fighters(dry_run=args.dry_run)
    summary_keys = [
        "gap_fighters", "resolved", "unresolved", "errors",
        "espn_has_photo", "espn_has_nat", "updated",
    ]
    print(json.dumps({k: counts.get(k, 0) for k in summary_keys}, indent=2))


if __name__ == "__main__":
    main()
