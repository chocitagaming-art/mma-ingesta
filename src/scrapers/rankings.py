"""Scrape the official UFC rankings (pound-for-pound + per-division top 15 + champions)
and load them into the `rankings` table.

Source note
-----------
The handoff targets ufcespanol.com, but that domain is hard-blocked at the edge
(Varnish 403 even on the homepage), so a JSON/API endpoint is unreachable from a
server. We therefore read the English ufc.com/rankings page, which is server-rendered
HTML (no JS needed) and exposes the *identical* official ranking. The data contract is
unaffected: division slugs are English (derived from the English headers) and fighter
names are not translated between the two sites. `RANKINGS_SOURCES` still tries
ufcespanol.com first, so it is used automatically if it ever becomes reachable.

We do not reuse `UfcStatsClient` from http.py because its anti-bot solver is specific to
ufcstats.com's SHA challenge; ufc.com just needs realistic browser headers + a warm
session. We keep the same good-citizen rate limiting via `Settings.request_delay_seconds`.

Name matching against the `fighters` table reuses espn.py's logic
(exact -> normalized -> fuzzy @ 0.92).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from difflib import get_close_matches

import requests
from bs4 import BeautifulSoup

from .config import Settings, get_settings
from .db import connect
from .espn import (
    FUZZY_MATCH_THRESHOLD,
    _build_exact_name_index,
    _build_normalized_name_index,
    _match_fighter,
    _normalize_name,
)
from .logging_config import configure_logging
from .matching import fold as _fold, ratio
from .repositories.fighters import get_all_fighters
from .repositories.rankings import (
    RankingRecord,
    delete_rankings_for_snapshot,
    ensure_ufc_promotion,
    insert_ranking,
)


LOGGER = logging.getLogger(__name__)

# (home_url, rankings_url) candidates, tried in order. The home is fetched first to
# warm cookies and look like a real navigation.
RANKINGS_SOURCES: list[tuple[str, str]] = [
    ("https://www.ufcespanol.com/", "https://www.ufcespanol.com/rankings"),
    ("https://www.ufc.com/", "https://www.ufc.com/rankings"),
]

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Sentinel stored in rank_change for fighters that (re-)entered the ranking ("NR"/NEW).
RANK_CHANGE_NEW = 999

# Scraped display name (normalized) -> canonical name in the fighters table, for cases
# where ufc.com embeds a nickname in the official name. Used only to resolve fighter_id;
# the contract's fighter_name column always stores the scraped name verbatim.
NAME_ALIASES: dict[str, str] = {
    "michael venom page": "Michael Page",
}

# Canonical division slugs (the data contract the frontend reads). Keyed by the
# cleaned, lowercased division header from ufc.com.
DIVISION_SLUGS: dict[str, str] = {
    "men's pound-for-pound": "mens_pound_for_pound",
    "women's pound-for-pound": "womens_pound_for_pound",
    "flyweight": "flyweight",
    "bantamweight": "bantamweight",
    "featherweight": "featherweight",
    "lightweight": "lightweight",
    "welterweight": "welterweight",
    "middleweight": "middleweight",
    "light heavyweight": "light_heavyweight",
    "heavyweight": "heavyweight",
    "women's strawweight": "womens_strawweight",
    "women's flyweight": "womens_flyweight",
    "women's bantamweight": "womens_bantamweight",
}

P4P_SLUGS = {"mens_pound_for_pound", "womens_pound_for_pound"}


@dataclass
class RankedEntry:
    rank_position: int
    fighter_name: str
    rank_change: int | None


@dataclass
class ParsedDivision:
    slug: str
    champion_name: str | None
    entries: list[RankedEntry] = field(default_factory=list)


# --------------------------------------------------------------------------- fetch


def _fetch_rankings_html(settings: Settings) -> tuple[str, str]:
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    last_error: Exception | None = None
    for home_url, rankings_url in RANKINGS_SOURCES:
        try:
            session.get(home_url, timeout=settings.request_timeout_seconds)
            time.sleep(settings.request_delay_seconds)
            response = session.get(rankings_url, timeout=settings.request_timeout_seconds)
            response.encoding = "utf-8"
            if response.status_code == 200 and "view-grouping" in response.text:
                LOGGER.info("Fetched UFC rankings from %s", rankings_url)
                return response.text, rankings_url
            LOGGER.warning(
                "Source %s unusable (status=%s, has_markup=%s)",
                rankings_url,
                response.status_code,
                "view-grouping" in response.text,
            )
        except requests.RequestException as exc:
            last_error = exc
            LOGGER.warning("Failed to fetch %s: %s", rankings_url, exc)
        time.sleep(settings.request_delay_seconds)
    raise RuntimeError(
        f"Could not fetch UFC rankings from any source. Last error: {last_error}"
    )


# --------------------------------------------------------------------------- parse


def _clean_header(header_el) -> str:
    # The header is e.g. "Men's Pound-for-Pound<span>Top Rank</span>"; drop the span.
    el = BeautifulSoup(str(header_el), "lxml")
    for span in el.select("span"):
        span.extract()
    text = el.get_text(" ", strip=True)
    return _normalize_header_text(text)


def _normalize_header_text(text: str) -> str:
    text = text.replace("’", "'")  # curly -> straight apostrophe
    text = re.sub(r"\btop rank\b", "", text, flags=re.IGNORECASE)
    return " ".join(text.split()).lower()


def _header_to_slug(cleaned_header: str) -> str | None:
    return DIVISION_SLUGS.get(cleaned_header)


def _parse_rank_change(cell) -> int | None:
    if cell is None:
        return None
    text = cell.get_text(" ", strip=True)
    if not text:
        return None
    classes = " ".join(
        span_class
        for span in cell.select("span")
        for span_class in span.get("class", [])
    )
    number_match = re.search(r"(\d+)", text)
    number = int(number_match.group(1)) if number_match else None
    if "rank-increase" in classes:
        return number
    if "rank-decrease" in classes:
        return -number if number is not None else None
    if "not-ranked" in classes or text.upper() == "NR":
        return RANK_CHANGE_NEW
    return None


def _parse_rankings(html: str, counts: Counter) -> list[ParsedDivision]:
    soup = BeautifulSoup(html, "lxml")
    # ufc.com renders each weight division's view-grouping TWICE (a desktop block and
    # a mobile block with identical content); the P4P groupings appear once. Parsing
    # every block verbatim emits duplicate (slug, rank_position) rows — the root cause
    # of the doubled 2026-06-24 snapshot. We collapse to one division per slug, keeping
    # the richer block, so the load stays a clean 13 divisions / ~206 rows.
    by_slug: dict[str, ParsedDivision] = {}
    for grouping in soup.select("div.view-grouping"):
        header_el = grouping.select_one("div.view-grouping-header")
        if header_el is None:
            continue
        cleaned = _clean_header(header_el)
        slug = _header_to_slug(cleaned)
        if slug is None:
            counts["unmapped_divisions"] += 1
            LOGGER.warning("Unmapped division header: %r (cleaned=%r)", header_el.get_text(strip=True), cleaned)
            continue

        champion_name: str | None = None
        if slug not in P4P_SLUGS:
            champion_link = grouping.select_one(".rankings--athlete--champion .info h5 a") or \
                grouping.select_one(".rankings--athlete--champion .info h5")
            if champion_link is not None:
                champion_name = champion_link.get_text(strip=True) or None
            if champion_name is None:
                LOGGER.warning("No champion found for division %s", slug)

        entries: list[RankedEntry] = []
        for row in grouping.select("table tbody tr"):
            name_cell = row.select_one("td.views-field-title")
            if name_cell is None:
                continue
            name = name_cell.get_text(strip=True)
            if not name:
                continue
            change_cell = row.select_one("td.views-field-weight-class-rank-change")
            # Rank is derived from DOM row order, which is authoritative. The page's
            # printed number is unreliable (ufc.com has duplicated e.g. "6" twice and
            # skipped "7" in some women's divisions); we only use it to flag mismatches.
            position = len(entries) + 1
            rank_cell = row.select_one("td.views-field-weight-class-rank")
            page_rank = rank_cell.get_text(strip=True) if rank_cell else ""
            if page_rank.isdigit() and int(page_rank) != position:
                counts["rank_mismatches"] += 1
                LOGGER.warning(
                    "Division %s: page rank %s != row position %s for %r; using row position",
                    slug, page_rank, position, name,
                )
            entries.append(
                RankedEntry(
                    rank_position=position,
                    fighter_name=name,
                    rank_change=_parse_rank_change(change_cell),
                )
            )

        parsed = ParsedDivision(slug=slug, champion_name=champion_name, entries=entries)
        existing = by_slug.get(slug)
        if existing is None:
            by_slug[slug] = parsed
        else:
            # Duplicate render of the same division: keep the richer block (more
            # ranked entries), but carry over a champion from whichever block has one
            # so an asymmetric/partial render can never drop rank 0. Reassigning an
            # existing dict key keeps the original first-seen ordering.
            counts["duplicate_groupings"] += 1
            kept = parsed if len(parsed.entries) > len(existing.entries) else existing
            kept.champion_name = kept.champion_name or existing.champion_name or parsed.champion_name
            by_slug[slug] = kept
            LOGGER.info(
                "Collapsed duplicate grouping for division %s (kept %d entries)",
                slug, len(kept.entries),
            )
    return list(by_slug.values())


# --------------------------------------------------------------------------- build + load


def _build_folded_index(fighters) -> dict[str, object]:
    """Map diacritic-folded name -> fighter. Folded keys that map to more than one
    distinct fighter (e.g. 'Jung-Yeob Lee' vs 'Jung Yeob Lee' both fold to the same
    key) are dropped as ambiguous, so the name simply falls through to fighter_id NULL
    (which the contract allows) instead of being steered to the wrong record."""
    index: dict[str, object] = {}
    ambiguous: set[str] = set()
    for fighter in fighters:
        if not fighter.name:
            continue
        key = _fold(fighter.name)
        existing = index.get(key)
        if existing is not None and existing.id != fighter.id:
            ambiguous.add(key)
        else:
            index[key] = fighter
    for key in ambiguous:
        index.pop(key, None)
    return index


def _match_fighter_folded(name: str, folded_index: dict[str, object]):
    """Secondary, diacritic-insensitive match used only when espn matching fails.
    Prefers an exact folded-key hit; the fuzzy fallback is guarded (same token count
    and same first name) to avoid attaching a wrong fighter_id to someone who is
    genuinely absent from the DB."""
    key = _fold(name)
    if not key:
        return None
    direct = folded_index.get(key)
    if direct is not None:
        return direct
    candidates = get_close_matches(key, folded_index.keys(), n=1, cutoff=FUZZY_MATCH_THRESHOLD)
    if not candidates:
        return None
    candidate = candidates[0]
    if ratio(key, candidate) < FUZZY_MATCH_THRESHOLD:
        return None
    key_tokens = key.split()
    candidate_tokens = candidate.split()
    if len(key_tokens) != len(candidate_tokens) or key_tokens[0] != candidate_tokens[0]:
        return None
    return folded_index[candidate]


def _build_records(
    divisions: list[ParsedDivision],
    promotion_id: int,
    snapshot_date: date,
    fighters,
    counts: Counter,
) -> list[RankingRecord]:
    exact_index = _build_exact_name_index(fighters)
    normalized_index = _build_normalized_name_index(fighters)
    folded_index = _build_folded_index(fighters)
    records: list[RankingRecord] = []

    def make_record(name: str, rank_position: int, is_champion: bool, rank_change: int | None) -> RankingRecord:
        lookup = NAME_ALIASES.get(_normalize_name(name), name)
        match = _match_fighter(lookup, exact_index, normalized_index)
        if match is None:
            match = _match_fighter_folded(lookup, folded_index)
            if match is not None:
                counts["matched_folded"] += 1
        if match is None:
            counts["unmatched"] += 1
            LOGGER.info("No fighter match for %r", name)
        else:
            counts["matched"] += 1
        return RankingRecord(
            fighter_id=match.id if match else None,
            promotion_id=promotion_id,
            division=slug,
            rank_position=rank_position,
            snapshot_date=snapshot_date,
            is_champion=is_champion,
            fighter_name=name,
            rank_change=rank_change,
        )

    for division in divisions:
        slug = division.slug
        if division.champion_name:
            records.append(make_record(division.champion_name, 0, True, None))
            counts["champions"] += 1
        for entry in division.entries:
            records.append(make_record(entry.fighter_name, entry.rank_position, False, entry.rank_change))
            counts["ranked"] += 1
        counts["divisions"] += 1
    return records


def scrape_rankings(snapshot_date: date | None = None, dry_run: bool = False) -> Counter:
    settings = get_settings()
    snapshot = snapshot_date or date.today()
    counts: Counter = Counter()
    counts["snapshot_date"] = snapshot.isoformat()

    html, source_url = _fetch_rankings_html(settings)
    counts["source_url"] = source_url
    divisions = _parse_rankings(html, counts)

    with connect(settings.database_url) as connection:
        promotion_id = ensure_ufc_promotion(connection)
        connection.commit()
        fighters = get_all_fighters(connection)
        counts["fighters_in_db"] = len(fighters)
        records = _build_records(divisions, promotion_id, snapshot, fighters, counts)
        counts["total_rows"] = len(records)

        if dry_run:
            LOGGER.info("Dry run: not writing %s rows", len(records))
            counts["written"] = 0
            return counts

        try:
            deleted = delete_rankings_for_snapshot(connection, promotion_id, snapshot)
            counts["deleted_existing"] = deleted
            for record in records:
                insert_ranking(connection, record)
            connection.commit()
            counts["written"] = len(records)
        except Exception:
            connection.rollback()
            raise
    return counts


def _build_summary(counts: Counter) -> str:
    keys = [
        "snapshot_date", "source_url", "divisions", "champions", "ranked",
        "total_rows", "matched", "matched_folded", "unmatched", "fighters_in_db",
        "unmapped_divisions", "duplicate_groupings", "rank_mismatches",
        "deleted_existing", "written",
    ]
    return json.dumps({key: counts.get(key, 0) for key in keys}, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape UFC rankings into the rankings table.")
    parser.add_argument("--dry-run", action="store_true", help="Parse + match but do not write to the DB.")
    parser.add_argument(
        "--snapshot-date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Override snapshot date (YYYY-MM-DD). Defaults to today.",
    )
    args = parser.parse_args()
    configure_logging()
    counts = scrape_rankings(snapshot_date=args.snapshot_date, dry_run=args.dry_run)
    print(_build_summary(counts))


if __name__ == "__main__":
    main()
