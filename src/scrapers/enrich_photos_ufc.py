"""Fill missing fighter data from official UFC athlete pages.

ESPN enrichment only covered ranked fighters, so most fighters render as
initials with an empty 0-0-0 record. UFC's own athlete pages
(ufc.com/athlete/<slug>) carry, for essentially every fighter, an official
headshot AND a bio block: pro record (W-L-D), height, weight, reach and place
of birth — plus (Phase 2) the full-body hero photo, leg reach and gym
("Trains at"). This resolves each gap fighter's page by name-slug and fills
ONLY empty fields (COALESCE for photo/measures/nationality; record only when it
is currently 0-0-0) so nothing already populated is overwritten.

Usage:
    python -m src.scrapers.enrich_photos_ufc --probe "Jon Jones" "Ilia Topuria"  # no DB, just test
    python -m src.scrapers.enrich_photos_ufc --dry-run            # report, no writes (upcoming-event fighters)
    python -m src.scrapers.enrich_photos_ufc --all                # ALL fighters missing photo or record (~thousands)
    python -m src.scrapers.enrich_photos_ufc --all --limit 300    # partial pass (re-runnable; resumes the gap)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .repositories.fighters import update_fighter_enrichment, update_fighter_record

LOGGER = logging.getLogger(__name__)

# ufcespanol.com returns Varnish 403 to scrapers; ufc.com serves the same
# official photos (UFC CDN) and responds 200.
ATHLETE_URL = "https://www.ufc.com/athlete/{slug}"
UFC_IMAGE_BASE = "https://www.ufc.com"
REQUEST_DELAY_SECONDS = 0.4
IN_TO_CM = 2.54
LB_TO_G = 453.59237
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# Full-body bio shot: used as a headshot fallback AND as the full_body_url fallback.
_FULL_BODY_RE = re.compile(r"/images/styles/athlete_bio_full_body/[^\s\"'<>()]+?\.png(?:\?[^\s\"'<>()]*)?", re.I)

# Preferred image styles, best first: a tight headshot, then bio body shot.
_IMAGE_RES = (
    re.compile(r"/images/styles/event_results_athlete_headshot/[^\s\"'<>()]+?\.png(?:\?[^\s\"'<>()]*)?", re.I),
    _FULL_BODY_RE,
    re.compile(r"/images/styles/event_fight_card_upper_body_of_standing_athlete/[^\s\"'<>()]+?\.png(?:\?[^\s\"'<>()]*)?", re.I),
)

# <p class="hero-profile__division-body">28-1-0 (W-L-D)</p>
RECORD_RE = re.compile(r"hero-profile__division-body[^>]*>\s*(\d+)-(\d+)-(\d+)")
_LABEL_RE = re.compile(r"c-bio__label[^>]*>\s*([^<]*?)\s*<")
_TEXT_RE = re.compile(r"c-bio__text[^>]*>\s*([^<]*?)\s*<")

# Full-body hero image: <div class="hero-profile__image-wrap"><img class="hero-profile__image" src="...">.
# Attribute order varies, so first grab the whole <img> tag by class, then its src.
_HERO_IMG_TAG_RE = re.compile(r"<img[^>]*\bhero-profile__image\b[^>]*>", re.IGNORECASE)
_SRC_RE = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

# <h1 class="hero-profile__name">Conor McGregor</h1> — the athlete's own name as
# the page renders it. Consumed by enrich_fullbody's anti-homonym guard: when a
# slug is guessed from a DB name, this is the proof the page belongs to that
# fighter and not to a namesake.
_HERO_NAME_RE = re.compile(r"hero-profile__name\b[^>]*>\s*([^<]+?)\s*<", re.IGNORECASE)


def slugify(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    ascii_name = ascii_name.lower().replace("'", "").replace(".", "")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")


_NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV"}
OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']""", re.IGNORECASE
)
# 'shadow'/'fighter_fulllength' catch UFC's generic full-length shadow
# silhouettes (e.g. SHADOW_Fighter_fullLength_RED, Fighter_fullLength_Shadow-woman-blue)
# whose filenames carry none of the older placeholder tokens.
_PLACEHOLDER_RE = re.compile(
    r"silhouette|nophoto|no-photo|default[-_]?(athlete|headshot)|shadow|fighter_fulllength",
    re.IGNORECASE,
)


def _is_placeholder_image(url: str | None) -> bool:
    """UFC/ESPN serve a generic silhouette for fighters with no real photo."""
    return bool(url) and _PLACEHOLDER_RE.search(url) is not None


@dataclass(frozen=True)
class AthleteData:
    headshot_url: str | None = None
    wins: int | None = None
    losses: int | None = None
    draws: int | None = None
    height_cm: float | None = None
    reach_cm: float | None = None
    weight_grams: int | None = None
    nationality: str | None = None
    full_body_url: str | None = None
    leg_reach_cm: float | None = None
    trains_at: str | None = None
    # Extended bio (Fase 4 / BE5, migration 010): the raw "Place of Birth"
    # field ("Chone, Ecuador" — nationality above keeps only the country) and
    # the "Octagon Debut" date ("Apr. 25, 2015").
    birth_place: str | None = None
    octagon_debut: date | None = None
    # Name as rendered by the page's hero block (None if the page had none).
    # NOT persisted; used to verify the page really belongs to the DB fighter.
    page_name: str | None = None

    @property
    def has_record(self) -> bool:
        return bool(self.wins or self.losses or self.draws)


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
    # Fallback: og:image is the canonical profile image for this athlete —
    # but UFC serves a generic SILHOUETTE.png for fighters with no real photo.
    og_match = OG_IMAGE_RE.search(html)
    if og_match:
        og = og_match.group(1)
        if og.startswith("http") and not _is_placeholder_image(og):
            return og
    return None


EVENT_URL = "https://www.ufc.com/event/{slug}"
_ATHLETE_HREF_RE = re.compile(r"/athlete/([^/?#]+)")


def athlete_slugs_from_event_html(html: str) -> dict[tuple[str, str], str]:
    """Real ufc.com slugs from an event page, keyed by (fight id, corner).

    WHY THIS EXISTS. The slug used to be GUESSED from the name stored in our DB
    (`slugify`), and for a debutant it is wrong about as often as it is right.
    Measured on the 29-ago-2026 card, 4 of the 5 problem fighters had a slug the
    guess could never reach:

        DB name            guessed            real (published by ufc.com)
        Hector Santiago    hector-santiago    hector-de-sousa-santiago
        Cameron Nelson     cameron-nelson     cam-nelson
        Ce Liu             ce-liu             liu-ce
        Xiao Long          xiao-long          shiyao-ron

    And ufc.com hands the right answer over for free: every corner of the event
    page is an <a href="/athlete/<slug>">. So we stop inventing it and read it.

    Keyed by (data-fmid, corner) and NOT by name on purpose — that is the same
    join `backfill_standing_photos` uses, and it sidesteps the whole name-matching
    problem: the page calls them "Liu Ce" and "Cam Nelson" while we call them
    "Ce Liu" and "Cameron Nelson", and data-fmid maps straight onto
    `fights.source_id`.
    """
    soup = BeautifulSoup(html, "html.parser")
    slugs: dict[tuple[str, str], str] = {}
    for bout in soup.select(".c-listing-fight"):
        fmid = (bout.get("data-fmid") or "").strip()
        if not fmid:
            continue
        for corner in ("red", "blue"):
            link = bout.select_one(f".c-listing-fight__corner-name--{corner} a[href]")
            if link is None:
                container = bout.select_one(f".c-listing-fight__corner--{corner}")
                link = container.select_one('a[href*="/athlete/"]') if container else None
            match = _ATHLETE_HREF_RE.search(link.get("href") or "") if link else None
            if match:
                slugs[(fmid, corner)] = match.group(1)
    return slugs


def _normalize_ufc_url(url: str | None) -> str | None:
    """Absolute https://www.ufc.com URL from whatever the page served (relative
    path, protocol-relative, http, or missing www)."""
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return UFC_IMAGE_BASE + url
    url = re.sub(r"^http://", "https://", url, count=1)
    return re.sub(r"^https://ufc\.com", UFC_IMAGE_BASE, url, count=1)


def _extract_full_body(html: str, name: str) -> str | None:
    """Full-body photo from the hero <img class="hero-profile__image">.

    The athlete_bio_full_body URLs are not constructible a priori — they must be
    read from the served HTML. Placeholder silhouettes are rejected (same guard
    as headshots). Fallback: any athlete_bio_full_body URL in the page whose
    filename carries this athlete's own name (LASTNAME_FIRSTNAME_*), mirroring
    _extract_headshot so an opponent's photo is never picked up.
    """
    for tag in _HERO_IMG_TAG_RE.findall(html):
        src_match = _SRC_RE.search(tag)
        if not src_match:
            continue
        url = _normalize_ufc_url(src_match.group(1))
        if url and not _is_placeholder_image(url):
            return url
    tokens = _name_tokens(name)
    if tokens:
        first, last = tokens[0], tokens[-1]
        for path in _FULL_BODY_RE.findall(html):
            upper = path.upper()
            if first in upper and last in upper and not _is_placeholder_image(path):
                return UFC_IMAGE_BASE + path
    return None


def _to_float(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


def _extract_bio_fields(html: str) -> dict[str, str]:
    # Parse each `c-bio__field` block on its own so an empty value never lets a
    # label steal the next field's text.
    fields: dict[str, str] = {}
    for block in html.split("c-bio__field")[1:]:
        label_m = _LABEL_RE.search(block)
        text_m = _TEXT_RE.search(block)
        if not (label_m and text_m):
            continue
        label = label_m.group(1).strip().lower()
        if label and label not in fields:
            fields[label] = text_m.group(1).strip()
    return fields


def _extract_page_name(html: str) -> str | None:
    """The athlete's name exactly as the served page renders it (hero block)."""
    match = _HERO_NAME_RE.search(html)
    if not match:
        return None
    name = re.sub(r"\s+", " ", match.group(1)).strip()
    return name or None


def _extract_record(html: str) -> tuple[int, int, int] | None:
    match = RECORD_RE.search(html)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _nationality_from_birthplace(place: str | None) -> str | None:
    # "Rochester, United States" -> "United States"; "France" -> "France".
    if not place:
        return None
    country = place.split(",")[-1].strip()
    return country or None


# ufc.com renders "Octagon Debut" as "Apr. 25, 2015" (abbreviated month with a
# trailing period); tolerate the unabbreviated ("April 25, 2015") and
# period-less ("Apr 25, 2015") variants too.
_DEBUT_FORMATS = ("%b %d, %Y", "%B %d, %Y")


def _parse_ufc_date(text: str | None) -> date | None:
    """Date from ufc.com's bio format, or None when absent/unparseable."""
    cleaned = " ".join((text or "").replace(".", " ").split())
    if not cleaned:
        return None
    for fmt in _DEBUT_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    LOGGER.debug("Unparseable ufc.com date: %r", text)
    return None


def resolve_athlete(
    session: requests.Session, name: str, slug: str | None = None
) -> AthleteData | None:
    """Read one ufc.com athlete page. `slug` overrides the name-guessed one."""
    url = ATHLETE_URL.format(slug=slug or slugify(name))
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
    # 🪤 AN UNKNOWN SLUG DOES NOT 404 — IT REDIRECTS TO /search AND RETURNS 200.
    # So a wrong guess used to come back as a perfectly OK page with no bio at
    # all, and the caller counted it as "resolved" with every field None. The
    # failure was completely mute: Hector Santiago (fighters.id 9123) was guessed
    # as /athlete/hector-santiago for as long as he existed — his real page is
    # /athlete/hector-de-sousa-santiago — and no counter, log line or alarm ever
    # said so. Detecting it here turns a silent nothing into `unresolved`, which
    # is visible in the run summary.
    # getattr, not response.url: a real requests Response always carries it, but
    # the test doubles in test_enrich_fullbody.py do not, and an AttributeError
    # here would take down a whole pass over a detail of a fake.
    if "/search" in urlsplit(getattr(response, "url", "") or "").path:
        LOGGER.info("Slug %r does not exist for %r (redirected to search)", slug or slugify(name), name)
        return None

    html = response.text
    fields = _extract_bio_fields(html)
    record = _extract_record(html)
    height = _to_float(fields.get("height"))
    reach = _to_float(fields.get("reach"))
    weight = _to_float(fields.get("weight"))
    leg_reach = _to_float(fields.get("leg reach"))
    return AthleteData(
        headshot_url=_extract_headshot(html, name),
        wins=record[0] if record else None,
        losses=record[1] if record else None,
        draws=record[2] if record else None,
        height_cm=round(height * IN_TO_CM, 1) if height else None,
        reach_cm=round(reach * IN_TO_CM, 1) if reach else None,
        weight_grams=int(round(weight * LB_TO_G)) if weight else None,
        nationality=_nationality_from_birthplace(fields.get("place of birth")),
        full_body_url=_extract_full_body(html, name),
        leg_reach_cm=round(leg_reach * IN_TO_CM, 1) if leg_reach else None,
        trains_at=fields.get("trains at") or None,
        birth_place=fields.get("place of birth") or None,
        octagon_debut=_parse_ufc_date(fields.get("octagon debut")),
        page_name=_extract_page_name(html),
    )


def resolve_headshot(session: requests.Session, name: str) -> str | None:
    data = resolve_athlete(session, name)
    return data.headshot_url if data else None


@dataclass(frozen=True)
class GapFighter:
    """A fighter to look up, plus where to find his REAL ufc.com slug.

    `event_slug`/`fmid`/`corner` are only populated for the upcoming-card scope:
    that is the one that can point at a live event page. The whole-table scope
    (`--all`) leaves them None and keeps guessing the slug from the name, which
    is all we have for a fighter who is not booked.
    """

    fighter_id: int
    name: str
    event_slug: str | None = None
    fmid: str | None = None
    corner: str | None = None


def _get_upcoming_gap_fighters(connection) -> list[GapFighter]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ON (f.id)
                   f.id,
                   f.name,
                   e.source_id AS event_slug,
                   fi.source_id AS fmid,
                   CASE WHEN fi.fighter_red_id = f.id THEN 'red' ELSE 'blue' END AS corner
            FROM events e
            JOIN fights fi ON fi.event_id = e.id
            JOIN fighters f
              ON (f.id = fi.fighter_red_id OR f.id = fi.fighter_blue_id)
            WHERE e.status = 'upcoming'
              AND (
                (f.headshot_url IS NULL OR f.headshot_url = '')
                OR (COALESCE(f.wins, 0) = 0 AND COALESCE(f.losses, 0) = 0 AND COALESCE(f.draws, 0) = 0)
              )
            -- DISTINCT ON keeps ONE bout per fighter to read the slug from, and
            -- the ORDER BY decides which: never a cancelled bout (its corner may
            -- not even be on the page any more), then the nearest event. The
            -- WHERE is deliberately unchanged, so the SET of fighters is exactly
            -- what it was before — only the extra context is new.
            ORDER BY f.id,
                     (fi.status IS DISTINCT FROM 'cancelled') DESC,
                     e.event_date ASC
            """
        )
        rows = [
            GapFighter(
                fighter_id=int(row[0]),
                name=str(row[1]),
                event_slug=row[2],
                fmid=str(row[3]) if row[3] is not None else None,
                corner=row[4],
            )
            for row in cursor.fetchall()
        ]
    # DISTINCT ON forces its own ORDER BY, so the caller's name order is restored
    # here. It only decides the order of the log lines, but a stable pass is
    # easier to compare between runs.
    return sorted(rows, key=lambda gap: gap.name)


def _get_all_gap_fighters(connection) -> list[GapFighter]:
    """Every fighter missing a photo OR with an empty 0-0-0 record."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name
            FROM fighters
            WHERE name IS NOT NULL AND name <> ''
              AND (
                (headshot_url IS NULL OR headshot_url = '')
                OR (COALESCE(wins, 0) = 0 AND COALESCE(losses, 0) = 0 AND COALESCE(draws, 0) = 0)
              )
            ORDER BY name
            """
        )
        return [GapFighter(fighter_id=int(row[0]), name=str(row[1])) for row in cursor.fetchall()]


def _event_slug_lookup(
    session: requests.Session, cache: dict[str, dict[tuple[str, str], str]], event_slug: str
) -> dict[tuple[str, str], str]:
    """Slugs of one event page, fetched at most once per run (empty on failure)."""
    if event_slug in cache:
        return cache[event_slug]
    url = EVENT_URL.format(slug=event_slug)
    slugs: dict[tuple[str, str], str] = {}
    try:
        response = session.get(url, headers=_HEADERS, timeout=15)
        if response.ok:
            slugs = athlete_slugs_from_event_html(response.text)
        else:
            LOGGER.info("HTTP %s for event page %s", response.status_code, url)
    except Exception as exc:  # noqa: BLE001 - a missing map only costs us the guess
        LOGGER.warning("Could not read event page %s: %s", url, exc)
    LOGGER.info("Event %s: %d athlete slugs read from the card", event_slug, len(slugs))
    cache[event_slug] = slugs
    return slugs


def _slug_for(
    session: requests.Session, cache: dict[str, dict[tuple[str, str], str]], gap: GapFighter
) -> str | None:
    """The slug ufc.com publishes for this fighter, or None to fall back to the guess."""
    if not (gap.event_slug and gap.fmid and gap.corner):
        return None
    return _event_slug_lookup(session, cache, gap.event_slug).get((gap.fmid, gap.corner))


def enrich(dry_run: bool = False, scope: str = "upcoming", limit: int | None = None) -> Counter:
    settings = get_settings()
    session = requests.Session()
    counts: Counter = Counter()
    with connect(settings.database_url) as connection:
        gaps = _get_all_gap_fighters(connection) if scope == "all" else _get_upcoming_gap_fighters(connection)
        LOGGER.info("Total fighters needing photo or record (scope=%s): %d", scope, len(gaps))
        if limit is not None:
            gaps = gaps[:limit]
        total = len(gaps)
        counts["gap_fighters"] = total
        LOGGER.info("Processing %d this run", total)

        slug_cache: dict[str, dict[tuple[str, str], str]] = {}

        for idx, gap in enumerate(gaps, 1):
            fighter_id, name = gap.fighter_id, gap.name
            slug = _slug_for(session, slug_cache, gap)
            if slug:
                counts["slug_from_card"] += 1
                if slug != slugify(name):
                    # The interesting half: the guess would have been wrong. Worth
                    # a line, because it is the only way to notice that our stored
                    # name and ufc.com's disagree.
                    counts["slug_guess_was_wrong"] += 1
                    LOGGER.info("Slug for %r: %s (the guess %s would have missed)", name, slug, slugify(name))
            data = resolve_athlete(session, name, slug=slug)
            time.sleep(REQUEST_DELAY_SECONDS)
            if data is None:
                counts["unresolved"] += 1
                continue
            counts["resolved"] += 1
            if data.headshot_url:
                counts["with_photo"] += 1
            if data.has_record:
                counts["with_record"] += 1

            if dry_run:
                continue

            enriched = update_fighter_enrichment(
                connection,
                fighter_id,
                headshot_url=data.headshot_url,
                nationality=data.nationality,
                height_cm=data.height_cm,
                reach_cm=data.reach_cm,
                weight_grams=data.weight_grams,
                full_body_url=data.full_body_url,
                leg_reach_cm=data.leg_reach_cm,
                trains_at=data.trains_at,
                birth_place=data.birth_place,
                octagon_debut=data.octagon_debut,
            )
            record_filled = False
            if data.has_record:
                record_filled = update_fighter_record(
                    connection, fighter_id, wins=data.wins, losses=data.losses, draws=data.draws
                )
            if enriched or record_filled:
                connection.commit()
                counts["updated"] += 1
            if record_filled:
                counts["record_filled"] += 1

            if idx % 50 == 0:
                LOGGER.info(
                    "Progress %d/%d — resolved=%d updated=%d record_filled=%d",
                    idx, total, counts["resolved"], counts["updated"], counts["record_filled"],
                )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill missing fighter photos + record/measures from UFC athlete pages.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve + report but do not write.")
    parser.add_argument("--all", action="store_true", dest="all_fighters", help="Process ALL fighters missing a photo or record, not just upcoming-event ones.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many fighters (for partial passes).")
    parser.add_argument("--probe", nargs="+", metavar="NAME", help="Test resolution for given names (no DB).")
    args = parser.parse_args()
    configure_logging()

    if args.probe:
        session = requests.Session()
        for name in args.probe:
            data = resolve_athlete(session, name)
            payload = {"name": name, "slug": slugify(name)}
            payload.update(data.__dict__ if data else {"resolved": False})
            # default=str: octagon_debut is a datetime.date.
            print(json.dumps(payload, ensure_ascii=False, default=str))
        return

    scope = "all" if args.all_fighters else "upcoming"
    counts = enrich(dry_run=args.dry_run, scope=scope, limit=args.limit)
    # slug_from_card / slug_guess_was_wrong ride in the summary on purpose: they
    # are the only place a wrong stored name shows up. `unresolved` used to be a
    # dead counter (a bad slug answered 200 on /search and counted as resolved),
    # so a run of zeros there meant nothing; now it means something.
    keys = [
        "gap_fighters", "resolved", "with_photo", "with_record", "updated",
        "record_filled", "unresolved", "slug_from_card", "slug_guess_was_wrong",
    ]
    print(json.dumps({key: counts.get(key, 0) for key in keys}, indent=2))


if __name__ == "__main__":
    main()
