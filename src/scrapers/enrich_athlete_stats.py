"""Scrape ufc.com career "athlete stats" and store them (migration 021).

The three hero tiles on a fighter profile — Wins by Knockout, Wins by
Submission and First Round Finishes — used to be computed ONLY from UFC fights
(table `fights`), so a fighter whose finishes are pre-UFC/regional showed 0/0/0
while ufc.com lists the CAREER totals (e.g. Anna Melisano: the profile said
0/0/0, ufc.com says 2/1/2). This pass reads the official career figures from the
ufc.com athlete hero block and stores them, so the profile shows career totals
consistent with the stored career W-L-D. The app falls back to the UFC-only
computation when we have no ufc.com data (prospects ufc.com does not list).

PARSING — the hero stats block is `div.hero-profile__stats` with one
`div.hero-profile__stat` per figure, each carrying a `.hero-profile__stat-numb`
(the number) and a `.hero-profile__stat-text` (the English label). Stats are
mapped BY LABEL, so column order never matters and an unknown extra stat is
ignored. A page without the block (historical fighters, 404) yields None and is
skipped — never written as a spurious 0/0/0.

ANTI-HOMONYM GUARD — same policy as enrich_facts / enrich_fullbody: the hero
name the page renders must match the DB fighter's name; a page with no hero name
is only trusted when the stored headshot already comes from ufc.com. Exact
duplicate names in the DB (the two Bruno Silva) share a slug and would cross-
attribute, so they are excluded from the scope.

WRITE POLICY — new-value-wins via update_fighter_finish_stats: ufc.com is
authoritative and career totals only grow, so the freshest scrape replaces
whatever is stored (a NULL triple never wipes; an identical triple is a no-op).

Usage:
    python -m src.scrapers.enrich_athlete_stats --probe "Cory Sandhagen"   # no DB
    python -m src.scrapers.enrich_athlete_stats --dry-run                  # upcoming cards, report only
    python -m src.scrapers.enrich_athlete_stats                            # upcoming-card fighters
    python -m src.scrapers.enrich_athlete_stats --all                      # whole-table backfill
    python -m src.scrapers.enrich_athlete_stats --all --limit 1000 --offset 1000   # chunked pass
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from .config import get_settings
from .db import connect
from .enrich_fullbody import _names_match
from .enrich_photos_ufc import ATHLETE_URL, REQUEST_DELAY_SECONDS, _HEADERS, _HERO_NAME_RE, slugify
from .logging_config import configure_logging
from .repositories.fighters import update_fighter_finish_stats

LOGGER = logging.getLogger(__name__)

PROGRESS_EVERY = 50

# ufc.com hero labels -> our column keys. Lower-cased, whitespace-collapsed.
_STAT_LABELS = {
    "wins by knockout": "ko",
    "wins by submission": "submission",
    "first round finishes": "first_round",
}


@dataclass(frozen=True)
class FinishStats:
    """The three career figures ufc.com renders in the hero stats block."""

    wins_by_ko: int
    wins_by_submission: int
    first_round_finishes: int


@dataclass(frozen=True)
class StatsPage:
    """Parsed athlete page: career finish stats (None if the block is absent)
    plus the hero name for the anti-homonym guard."""

    stats: FinishStats | None
    page_name: str | None


# resolver(session, name) -> StatsPage | None. Injected in tests (no network).
Resolver = Callable[[requests.Session, str], "StatsPage | None"]
# Target row: (fighter_id, name, ufc_confirmed) — same shape as enrich_facts.
Target = tuple[int, str, bool]


def _clean_text(text: str) -> str:
    """NFKC (folds any ligature ufc.com serves), NBSP -> space, collapse ws."""
    normalized = unicodedata.normalize("NFKC", text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def _parse_int(text: str | None) -> int | None:
    """The integer inside a stat number cell ('0' is a valid reading)."""
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def parse_finish_stats(soup: BeautifulSoup) -> FinishStats | None:
    """Career KO / submission / first-round-finish counts from the hero block.

    Returns None when the block is absent (historical fighters / pages without
    stats): the caller must never persist that as 0/0/0. When the block exists,
    a specific figure missing from it defaults to 0 (a real 0 the page omitted).
    """
    container = soup.select_one("div.hero-profile__stats")
    if container is None:
        return None
    found: dict[str, int] = {}
    for stat in container.select("div.hero-profile__stat"):
        numb = stat.select_one(".hero-profile__stat-numb")
        text = stat.select_one(".hero-profile__stat-text")
        if numb is None or text is None:
            continue
        key = _STAT_LABELS.get(_clean_text(text.get_text(" ", strip=True)).lower())
        if key is None:
            continue
        value = _parse_int(numb.get_text(" ", strip=True))
        if value is not None:
            found[key] = value
    if not found:
        # Container present but no recognized stat (layout drift): treat as
        # "no data" rather than writing zeros.
        return None
    return FinishStats(
        wins_by_ko=found.get("ko", 0),
        wins_by_submission=found.get("submission", 0),
        first_round_finishes=found.get("first_round", 0),
    )


def resolve_stats(session: requests.Session, name: str) -> StatsPage | None:
    """Fetch the athlete page by name slug and parse stats + hero name. Returns
    None on network errors and non-200s (404 = fighter without a page)."""
    slug = slugify(name)
    if not slug:
        return None
    try:
        response = session.get(ATHLETE_URL.format(slug=slug), headers=_HEADERS, timeout=30)
    except requests.RequestException:
        return None
    if not response.ok:
        return None
    html = response.text
    soup = BeautifulSoup(html, "lxml")
    match = _HERO_NAME_RE.search(html)
    page_name = _clean_text(match.group(1)) if match else None
    return StatsPage(stats=parse_finish_stats(soup), page_name=page_name)


def _identity_verified(db_name: str, page: StatsPage, ufc_confirmed: bool) -> bool:
    """Same anti-homonym policy as enrich_facts._identity_verified."""
    if page.page_name is None:
        return ufc_confirmed
    return _names_match(db_name, page.page_name)


def _get_target_fighters(
    connection,
    limit: int | None = None,
    offset: int = 0,
    all_scope: bool = False,
) -> list[Target]:
    """Fighters to (re)scrape. Unlike facts (additive, so it targets NULL rows),
    finish stats are new-value-wins, so the scope is NOT filtered by NULL — every
    fighter in scope is refreshed and the writer skips unchanged rows.

    Default scope: fighters booked on an upcoming event (small, mirrors the cron;
    stats can grow right before a fight). ``all_scope``: the whole table, ordered
    by product priority so partial --limit/--offset passes cover the most visible
    fighters first. Exact-duplicate names are excluded (homonym safety).
    """
    homonym_free = """
              AND NOT EXISTS (
                SELECT 1 FROM fighters dup
                WHERE dup.id <> f.id AND lower(dup.name) = lower(f.name)
              )
    """
    if all_scope:
        sql = f"""
            SELECT f.id, f.name, (f.headshot_url ILIKE %s) AS ufc_confirmed
            FROM fighters f
            WHERE f.name IS NOT NULL AND f.name <> ''
              {homonym_free}
            ORDER BY
                EXISTS (
                    SELECT 1 FROM rankings r
                    WHERE r.fighter_id = f.id
                      AND r.snapshot_date = (SELECT MAX(snapshot_date) FROM rankings)
                ) DESC,
                EXISTS (
                    SELECT 1 FROM fights fi
                    JOIN events e ON e.id = fi.event_id
                    WHERE e.status = 'upcoming'
                      AND (fi.fighter_red_id = f.id OR fi.fighter_blue_id = f.id)
                ) DESC,
                (NULLIF(f.headshot_url, '') IS NOT NULL) DESC,
                f.name, f.id
        """
    else:
        sql = f"""
            SELECT DISTINCT f.id, f.name, (f.headshot_url ILIKE %s) AS ufc_confirmed
            FROM fighters f
            JOIN fights fi ON fi.fighter_red_id = f.id OR fi.fighter_blue_id = f.id
            JOIN events e ON e.id = fi.event_id
            WHERE e.status = 'upcoming'
              AND f.name IS NOT NULL AND f.name <> ''
              {homonym_free}
            ORDER BY f.name, f.id
        """
    params: list = ["%ufc.com%"]
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    if offset:
        sql += " OFFSET %s"
        params.append(offset)
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        return [(int(row[0]), str(row[1]), bool(row[2])) for row in cursor.fetchall()]


def backfill(
    connection,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    offset: int = 0,
    all_scope: bool = False,
    resolver: Resolver = resolve_stats,
    sleeper: Callable[[float], None] = time.sleep,
) -> Counter:
    session = requests.Session()
    counts: Counter = Counter()
    targets = _get_target_fighters(connection, limit=limit, offset=offset, all_scope=all_scope)
    total = len(targets)
    counts["targets"] = total
    LOGGER.info(
        "Fighters to scrape for finish stats (scope=%s): %d",
        "all" if all_scope else "upcoming", total,
    )

    for idx, (fighter_id, name, ufc_confirmed) in enumerate(targets, 1):
        page = resolver(session, name)
        sleeper(REQUEST_DELAY_SECONDS)
        if page is None:
            # Includes ufc.com 404s (fighters without a page): expected.
            counts["unresolved"] += 1
        elif not _identity_verified(name, page, ufc_confirmed):
            counts["name_mismatch"] += 1
            LOGGER.warning(
                "Name mismatch for fighter id=%d %r: page renders %r — skipping",
                fighter_id, name, page.page_name,
            )
        elif page.stats is None:
            # Page exists but has no hero stats block (most historical fighters).
            counts["no_stats"] += 1
        else:
            counts["with_stats"] += 1
            if dry_run:
                counts["would_update"] += 1
                LOGGER.info(
                    "[dry-run] id=%d %r: KO=%d SUB=%d 1R=%d",
                    fighter_id, name, page.stats.wins_by_ko,
                    page.stats.wins_by_submission, page.stats.first_round_finishes,
                )
            else:
                updated = update_fighter_finish_stats(
                    connection,
                    fighter_id,
                    wins_by_ko=page.stats.wins_by_ko,
                    wins_by_submission=page.stats.wins_by_submission,
                    first_round_finishes=page.stats.first_round_finishes,
                )
                if updated:
                    connection.commit()
                    counts["updated"] += 1

        if idx % PROGRESS_EVERY == 0:
            LOGGER.info(
                "Progress %d/%d — with_stats=%d updated=%d no_stats=%d "
                "unresolved=%d name_mismatch=%d",
                idx, total, counts["with_stats"], counts["updated"],
                counts["no_stats"], counts["unresolved"], counts["name_mismatch"],
            )
    if dry_run:
        # Release the read-only snapshot; guarantees dry-run never commits.
        connection.rollback()
    return counts


def probe(names: list[str]) -> None:
    """No-DB check: fetch + parse and print the result as JSON."""
    session = requests.Session()
    for name in names:
        page = resolve_stats(session, name)
        time.sleep(REQUEST_DELAY_SECONDS)
        if page is None:
            print(json.dumps({"name": name, "resolved": False}, ensure_ascii=False))
            continue
        result: dict = {
            "name": name,
            "resolved": True,
            "page_name": page.page_name,
            "stats": page.stats.__dict__ if page.stats else None,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape ufc.com career finish stats (KO/Sub/1st-round) and store them."
    )
    parser.add_argument("--dry-run", action="store_true", help="Resolve + report, no writes.")
    parser.add_argument("--all", action="store_true", dest="all_scope", help="Whole-table scope (default: upcoming cards only).")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many fighters (chunked passes).")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many fighters first (chunk --all with --limit).")
    parser.add_argument("--probe", nargs="+", default=None, help="No DB: fetch/parse these names and print JSON.")
    args = parser.parse_args()
    configure_logging()

    if args.probe:
        probe(args.probe)
        return

    settings = get_settings()
    with connect(settings.database_url) as connection:
        counts = backfill(
            connection,
            dry_run=args.dry_run,
            limit=args.limit,
            offset=args.offset,
            all_scope=args.all_scope,
        )

    keys = ["targets", "with_stats", "updated", "would_update", "no_stats", "unresolved", "name_mismatch"]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")


if __name__ == "__main__":
    main()
