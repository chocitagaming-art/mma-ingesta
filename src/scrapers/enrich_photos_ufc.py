"""Fill missing fighter headshots from official UFC athlete pages.

ESPN enrichment only covered ranked fighters, so fighters who appear only in
upcoming-event cards render as initials. UFC's own athlete pages
(ufcespanol.com/athlete/<slug>) carry an official headshot for essentially every
active fighter. This resolves each gap fighter's page by name-slug, extracts the
`event_results_athlete_headshot` image, and fills ONLY a NULL headshot_url
(update_fighter_enrichment uses COALESCE, so nothing is overwritten).

Usage:
    python -m src.scrapers.enrich_photos_ufc --probe "Ion Cutelaba" "Navajo Stirling"  # no DB, just test
    python -m src.scrapers.enrich_photos_ufc --dry-run    # list gap fighters, no writes
    python -m src.scrapers.enrich_photos_ufc              # enrich upcoming-event fighters (writes)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import unicodedata
from collections import Counter

import requests

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .repositories.fighters import update_fighter_enrichment

LOGGER = logging.getLogger(__name__)

# ufcespanol.com returns Varnish 403 to scrapers; ufc.com serves the same
# official photos (UFC CDN) and responds 200.
ATHLETE_URL = "https://www.ufc.com/athlete/{slug}"
UFC_IMAGE_BASE = "https://www.ufc.com"
REQUEST_DELAY_SECONDS = 0.4
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# Preferred image styles, best first: a tight headshot, then bio body shot.
_IMAGE_RES = (
    re.compile(r"/images/styles/event_results_athlete_headshot/[^\s\"'<>()]+?\.png(?:\?[^\s\"'<>()]*)?", re.I),
    re.compile(r"/images/styles/athlete_bio_full_body/[^\s\"'<>()]+?\.png(?:\?[^\s\"'<>()]*)?", re.I),
    re.compile(r"/images/styles/event_fight_card_upper_body_of_standing_athlete/[^\s\"'<>()]+?\.png(?:\?[^\s\"'<>()]*)?", re.I),
)


def slugify(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    ascii_name = ascii_name.lower().replace("'", "").replace(".", "")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")


_NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV"}
OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']""", re.IGNORECASE
)


def _name_tokens(name: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return [t for t in re.findall(r"[A-Za-z0-9]+", ascii_name.upper()) if t not in _NAME_SUFFIXES]


def _extract_headshot(html: str, name: str) -> str | None:
    # UFC pages embed other fighters' photos (opponents, related fights), so the
    # FIRST headshot is not necessarily this athlete. Match the image whose
    # filename contains the fighter's own name (files are LASTNAME_FIRSTNAME_*).
    candidates: list[str] = []
    for pattern in _IMAGE_RES:
        candidates.extend(pattern.findall(html))
    tokens = _name_tokens(name)
    if tokens:
        first, last = tokens[0], tokens[-1]
        for path in candidates:
            upper = path.upper()
            if first in upper and last in upper:
                return UFC_IMAGE_BASE + path
    # Fallback: og:image is the canonical profile image for this athlete.
    og_match = OG_IMAGE_RE.search(html)
    if og_match and og_match.group(1).startswith("http"):
        return og_match.group(1)
    return None


def resolve_headshot(session: requests.Session, name: str) -> str | None:
    url = ATHLETE_URL.format(slug=slugify(name))
    try:
        response = session.get(url, headers=_HEADERS, timeout=15)
    except Exception as exc:  # noqa: BLE001 - network issues are non-fatal
        LOGGER.warning("Fetch failed for %r (%s): %s", name, url, exc)
        return None
    if response.status_code == 404:
        return None
    if not response.ok:
        LOGGER.info("HTTP %s for %r (%s)", response.status_code, name, url)
        return None
    return _extract_headshot(response.text, name)


def _get_upcoming_gap_fighters(connection) -> list[tuple[int, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT f.id, f.name
            FROM events e
            JOIN fights fi ON fi.event_id = e.id
            JOIN fighters f
              ON (f.id = fi.fighter_red_id OR f.id = fi.fighter_blue_id)
            WHERE e.status = 'upcoming'
              AND (f.headshot_url IS NULL OR f.headshot_url = '')
            ORDER BY f.name
            """
        )
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def _get_all_gap_fighters(connection) -> list[tuple[int, str]]:
    """Every fighter missing a photo, not just upcoming-event ones (~2.6k rows)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name
            FROM fighters
            WHERE (headshot_url IS NULL OR headshot_url = '')
              AND name IS NOT NULL AND name <> ''
            ORDER BY name
            """
        )
        return [(int(row[0]), str(row[1])) for row in cursor.fetchall()]


def enrich(dry_run: bool = False, scope: str = "upcoming", limit: int | None = None) -> Counter:
    settings = get_settings()
    session = requests.Session()
    counts: Counter = Counter()
    with connect(settings.database_url) as connection:
        gaps = _get_all_gap_fighters(connection) if scope == "all" else _get_upcoming_gap_fighters(connection)
        LOGGER.info("Total fighters missing a photo (scope=%s): %d", scope, len(gaps))
        if limit is not None:
            gaps = gaps[:limit]
        total = len(gaps)
        counts["gap_fighters"] = total
        LOGGER.info("Processing %d this run", total)
        for idx, (fighter_id, name) in enumerate(gaps, 1):
            headshot = resolve_headshot(session, name)
            time.sleep(REQUEST_DELAY_SECONDS)
            if not headshot:
                counts["unresolved"] += 1
                continue
            counts["resolved"] += 1
            if not dry_run and update_fighter_enrichment(connection, fighter_id, headshot_url=headshot):
                connection.commit()
                counts["updated"] += 1
            if idx % 50 == 0:
                LOGGER.info("Progress %d/%d — resolved=%d updated=%d", idx, total, counts["resolved"], counts["updated"])
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill missing fighter headshots from UFC athlete pages.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve + report but do not write.")
    parser.add_argument("--all", action="store_true", dest="all_fighters", help="Process ALL fighters missing a photo (~2.6k), not just upcoming-event ones.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many fighters (for partial passes).")
    parser.add_argument("--probe", nargs="+", metavar="NAME", help="Test resolution for given names (no DB).")
    args = parser.parse_args()
    configure_logging()

    if args.probe:
        session = requests.Session()
        for name in args.probe:
            url = resolve_headshot(session, name)
            print(json.dumps({"name": name, "slug": slugify(name), "headshot": url}))
        return

    scope = "all" if args.all_fighters else "upcoming"
    counts = enrich(dry_run=args.dry_run, scope=scope, limit=args.limit)
    keys = ["gap_fighters", "resolved", "unresolved", "updated"]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))


if __name__ == "__main__":
    main()
