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

ANTI-HOMONYM GUARD — same policy as enrich_facts / enrich_fullbody (shared
enrich_fullbody._hero_name_matches): the hero name the page renders must match
the DB fighter's name, or be exactly their nickname (2+ words, shared by no other
fighter; counted as ``nickname_match`` and logged); a page with no
hero name is only trusted when the stored headshot already comes from ufc.com. Exact
duplicate names in the DB (the two Bruno Silva) share a slug and would cross-
attribute, so they are excluded from the scope.

WRITE POLICY — new-value-wins via update_fighter_finish_stats: ufc.com is
authoritative and career totals only grow, so the freshest scrape replaces
whatever is stored (a NULL triple never wipes; an identical triple is a no-op).

SOCIAL LINKS (migration 032, R6) — the SAME page also carries the fighter's
Instagram and X in its JSON-LD Person.sameAs (src/scrapers/ufc_social.py: never
the page anchors, whose footer links @ufc). Parsed from the soup already in
hand, so zero extra requests. Written behind the same anti-homonym guard and
also for pages WITHOUT the stats block (retired fighters have socials too).
Rule (owner, 18-oct-2026): Person block present -> store what it lists and
NULL what it no longer lists; no block -> touch nothing. If migration 032 is
not applied yet the run still refreshes stats and only counts socials.

🪤 THE BRAKE. "NULL what it no longer lists" means one ufc.com template change
(sameAs renamed, a new X URL shape) would wipe the links of EVERY fighter in a
single sweep, in green. So links are SET inline, fighter by fighter, but the
deletions are only collected, and applied in one statement per network at the
END of the run, and only if the run looks like ufc.com still publishes them:
with at least SOCIALS_BRAKE_MIN_PAGES Person blocks seen, a network listed on
fewer than SOCIALS_BRAKE_MIN_SHARE of them (or Person blocks missing on more
than half of the verified pages) holds those deletions back, prints
``socials_brake=1`` and exits 1, which notify-on-failure turns into an alert.
A link ufc.com lists in a shape we cannot read is never deleted
(``socials_unparsed``). Measured 18-oct-2026 on 72 Person blocks drawn at
random from the --all scope: Instagram 0.56, X 0.67, any 0.76, no block 0/72;
upcoming cards (20 pages): Instagram 0.90, X 0.80.

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
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from .config import get_settings
from .db import connect
from .enrich_fullbody import _accepted_by_nickname, _hero_name_matches, _unique_nickname_sql
from .enrich_photos_ufc import ATHLETE_URL, REQUEST_DELAY_SECONDS, _HEADERS, _HERO_NAME_RE, slugify
from .logging_config import configure_logging
from .repositories.fighters import (
    clear_fighter_social_links,
    count_fighter_social_links_set,
    fighter_social_columns_present,
    update_fighter_finish_stats,
    update_fighter_social_links,
)
from .ufc_social import SocialLinks, extract_social_links

LOGGER = logging.getLogger(__name__)

# Run-level brake on deleting social links (see THE BRAKE above). Below
# MIN_PAGES Person blocks the run is too small to judge and deletes as usual
# (a handful of fighters at most). 0.20 sits far below every share measured
# (0.56 the lowest) and far above the ~0 a template change produces.
SOCIALS_BRAKE_MIN_PAGES = 30
SOCIALS_BRAKE_MIN_SHARE = 0.20
# (column, summary key of the fighters whose page lists it)
_SOCIAL_NETWORKS = (("instagram_url", "with_instagram"), ("x_url", "with_x"))

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
    """Parsed athlete page: career finish stats (None if the block is absent),
    the hero name for the anti-homonym guard, and the social links (None if the
    JSON-LD Person block is absent: then they must not be touched)."""

    stats: FinishStats | None
    page_name: str | None
    socials: SocialLinks | None = None


# resolver(session, name) -> StatsPage | None. Injected in tests (no network).
Resolver = Callable[[requests.Session, str], "StatsPage | None"]
# Target row: (fighter_id, name, nickname, ufc_confirmed) — same shape as enrich_facts.
Target = tuple[int, str, str | None, bool]


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
    # An unknown slug does not 404: ufc.com redirects to /search with a 200 (see
    # enrich_photos_ufc.resolve_athlete). That page has no hero name, so the
    # guard would trust it for an ufc_confirmed fighter: count it as unresolved.
    if "/search" in urlsplit(getattr(response, "url", "") or "").path:
        return None
    html = response.text
    soup = BeautifulSoup(html, "lxml")
    match = _HERO_NAME_RE.search(html)
    page_name = _clean_text(match.group(1)) if match else None
    return StatsPage(
        stats=parse_finish_stats(soup),
        page_name=page_name,
        socials=extract_social_links(soup),
    )


def _identity_verified(
    db_name: str, page: StatsPage, ufc_confirmed: bool, *, nickname: str | None
) -> bool:
    """Same anti-homonym policy as enrich_facts._identity_verified."""
    if page.page_name is None:
        return ufc_confirmed
    return _hero_name_matches(db_name, nickname, page.page_name)


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
    nickname = _unique_nickname_sql("f.nickname")
    if all_scope:
        sql = f"""
            SELECT f.id, f.name, {nickname}, (f.headshot_url ILIKE %s) AS ufc_confirmed
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
            SELECT DISTINCT f.id, f.name, {nickname}, (f.headshot_url ILIKE %s) AS ufc_confirmed
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
        return [
            (int(row[0]), str(row[1]), row[2] or None, bool(row[3]))
            for row in cursor.fetchall()
        ]


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
    # Fighters whose verified page no longer lists a network: deleted at the
    # end of the run, behind the brake (_settle_social_clears).
    pending_clears: dict[str, list[int]] = {column: [] for column, _ in _SOCIAL_NETWORKS}
    targets = _get_target_fighters(connection, limit=limit, offset=offset, all_scope=all_scope)
    # Asked once, inside the same read transaction as the target SELECT.
    socials_writable = fighter_social_columns_present(connection)
    if not socials_writable:
        counts["socials_columns_missing"] = 1
        LOGGER.warning(
            "fighters has no instagram_url/x_url yet (migration 032 not applied): "
            "social links are parsed and counted but NOT written"
        )
    # Release the read transaction of that SELECT right away. Neon kills a
    # session left idle INSIDE a transaction for 5 min
    # (idle_in_transaction_session_timeout=5min) and the sweep then spends half
    # an hour on ufc.com: enrich-facts died exactly like that on 1-sep-2026
    # (run 33484806063, "SSL connection has been closed unexpectedly").
    connection.rollback()
    total = len(targets)
    counts["targets"] = total
    LOGGER.info(
        "Fighters to scrape for finish stats (scope=%s): %d",
        "all" if all_scope else "upcoming", total,
    )

    for idx, (fighter_id, name, nickname, ufc_confirmed) in enumerate(targets, 1):
        page = resolver(session, name)
        sleeper(REQUEST_DELAY_SECONDS)
        if page is None:
            # Includes ufc.com 404s (fighters without a page): expected.
            counts["unresolved"] += 1
        elif not _identity_verified(name, page, ufc_confirmed, nickname=nickname):
            counts["name_mismatch"] += 1
            LOGGER.warning(
                "Name mismatch for fighter id=%d %r: page renders %r — skipping",
                fighter_id, name, page.page_name,
            )
        else:
            if _accepted_by_nickname(name, page.page_name):
                counts["nickname_match"] += 1
                LOGGER.info(
                    "Accepted by nickname: fighter id=%d %r (nickname %r) — page renders %r",
                    fighter_id, name, nickname, page.page_name,
                )
            if page.stats is None:
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
                        counts["updated"] += 1
            _handle_socials(
                connection, counts, pending_clears, fighter_id, name, page.socials,
                dry_run=dry_run, writable=socials_writable,
            )

        if not dry_run:
            # Close the transaction on EVERY iteration, also when the UPDATE hit
            # 0 rows (IS DISTINCT FROM: nothing new): that UPDATE still opened a
            # transaction, and carrying it across the next fetches is the same
            # 5-min Neon trap. psycopg2 sends nothing when no transaction is
            # open, so iterations without SQL cost nothing. Dry-run runs no SQL
            # in this loop, so the rollback above leaves nothing to close, and
            # it never commits (connect() closes/rolls back, never commits).
            connection.commit()

        if idx % PROGRESS_EVERY == 0:
            LOGGER.info(
                "Progress %d/%d — with_stats=%d updated=%d no_stats=%d "
                "unresolved=%d name_mismatch=%d with_socials=%d socials_updated=%d",
                idx, total, counts["with_stats"], counts["updated"],
                counts["no_stats"], counts["unresolved"], counts["name_mismatch"],
                counts["with_socials"], counts["socials_updated"],
            )
    _settle_social_clears(
        connection, counts, pending_clears, dry_run=dry_run, writable=socials_writable
    )
    return counts


def _handle_socials(
    connection,
    counts: Counter,
    pending_clears: dict[str, list[int]],
    fighter_id: int,
    name: str,
    socials: SocialLinks | None,
    *,
    dry_run: bool,
    writable: bool,
) -> None:
    """Count one verified page's social links, store the ones it lists (unless
    dry-run / migration missing) and queue the deletion of the ones it no
    longer lists. Called ONLY after the anti-homonym guard passed."""
    if socials is None:
        # No JSON-LD Person block: "we do not know", never "no socials".
        counts["no_socials_block"] += 1
        return
    counts["socials_block"] += 1
    if socials.instagram_url:
        counts["with_instagram"] += 1
    if socials.x_url:
        counts["with_x"] += 1
    if socials.any:
        counts["with_socials"] += 1
    for column, url, unparsed in (
        ("instagram_url", socials.instagram_url, socials.instagram_unparsed),
        ("x_url", socials.x_url, socials.x_unparsed),
    ):
        if url is not None:
            continue
        if unparsed:
            # Listed, but in a shape we cannot read: keep what is stored.
            counts["socials_unparsed"] += 1
            LOGGER.warning(
                "Unreadable %s in ufc.com sameAs for id=%d %r: kept as stored",
                column, fighter_id, name,
            )
        else:
            pending_clears[column].append(fighter_id)
    if dry_run:
        LOGGER.info(
            "[dry-run] id=%d %r: instagram=%s x=%s",
            fighter_id, name, socials.instagram_url, socials.x_url,
        )
        return
    if not writable or not socials.any:
        return
    if update_fighter_social_links(
        connection, fighter_id, instagram_url=socials.instagram_url, x_url=socials.x_url
    ):
        counts["socials_updated"] += 1


def _socials_brake_reason(counts: Counter, with_key: str | None) -> str | None:
    """Why this run must not delete links (None = it may). ``with_key`` None
    asks only the run-wide question (Person blocks missing)."""
    block, no_block = counts["socials_block"], counts["no_socials_block"]
    if block + no_block >= SOCIALS_BRAKE_MIN_PAGES and no_block * 2 > block + no_block:
        return f"no JSON-LD Person block on {no_block} of {block + no_block} verified pages"
    if with_key is None or block < SOCIALS_BRAKE_MIN_PAGES:
        return None
    if counts[with_key] < SOCIALS_BRAKE_MIN_SHARE * block:
        return f"{with_key}={counts[with_key]} of socials_block={block}"
    return None


def _settle_social_clears(
    connection,
    counts: Counter,
    pending_clears: dict[str, list[int]],
    *,
    dry_run: bool,
    writable: bool,
) -> None:
    """End of run: delete the links ufc.com no longer lists, one statement per
    network, unless the brake says the run looks like a ufc.com change."""
    run_wide = _socials_brake_reason(counts, None)
    if run_wide:
        # No deletion is at stake here, but the fill has silently stopped.
        counts["socials_brake"] = 1
        LOGGER.error("Social-links brake: %s", run_wide)
    for column, with_key in _SOCIAL_NETWORKS:
        ids = pending_clears[column]
        if not ids:
            continue
        reason = _socials_brake_reason(counts, with_key)
        if reason:
            counts["socials_brake"] = 1
            counts["socials_clears_held"] += len(ids)
            LOGGER.error(
                "Social-links brake: %s; NOT deleting %s for %d fighters "
                "(did ufc.com change its JSON-LD?)",
                reason, column, len(ids),
            )
            continue
        if not writable:
            continue
        if dry_run:
            counts["socials_would_clear"] += count_fighter_social_links_set(
                connection, column, ids
            )
            continue
        counts["socials_cleared"] += clear_fighter_social_links(connection, column, ids)
        connection.commit()


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
            "socials": page.socials.__dict__ if page.socials else None,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape ufc.com career finish stats (KO/Sub/1st-round) and the "
            "fighter's Instagram/X links, and store them."
        )
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

    keys = [
        "targets", "with_stats", "updated", "would_update", "no_stats", "unresolved",
        "name_mismatch", "nickname_match",
        # Social links (migration 032). with_socials falling to ~0 on a run that
        # still resolves pages = ufc.com changed or dropped its JSON-LD block.
        "socials_block", "no_socials_block", "with_socials", "with_instagram", "with_x",
        "socials_updated", "socials_cleared", "socials_would_clear", "socials_unparsed",
        "socials_clears_held", "socials_brake", "socials_columns_missing",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))
    if args.dry_run:
        print("Dry-run: nothing was written. Re-run without --dry-run to persist.")
    if counts.get("socials_brake"):
        # Stats were refreshed and links SET as usual; only the deletions were
        # held. Exit 1 so notify-on-failure raises the alarm.
        print("socials_brake=1: social-link deletions held back (see the log). Exit 1.")
        sys.exit(1)


if __name__ == "__main__":
    main()
