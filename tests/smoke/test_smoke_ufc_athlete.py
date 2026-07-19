"""Smoke: a ufc.com athlete page still yields hero stats, record and photos.

ONE download of an anchor athlete page validated with BOTH parse families,
protecting the four enrich-* crons that read athlete pages:
  - enrich_athlete_stats.parse_finish_stats (hero stats block) + the hero-name
    regex every anti-homonym guard relies on (enrich_facts/fullbody/stats);
  - enrich_photos_ufc's RECORD_RE (W-L-D) and _IMAGE_RES photo styles.

Anchor: Jon Jones — retired, record frozen at 28-1-0, page stable and fully
populated (headshot + hero stats verified 2026-07).
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from src.scrapers.enrich_athlete_stats import parse_finish_stats
from src.scrapers.enrich_photos_ufc import (
    ATHLETE_URL,
    RECORD_RE,
    _HERO_NAME_RE,
    _IMAGE_RES,
    _is_placeholder_image,
)


pytestmark = pytest.mark.smoke

ATHLETE_ANCHOR_SLUG = "jon-jones"


def test_athlete_page_stats_record_and_photos(ufc_session, smoke_settings, retry_fetch):
    """ONE download, both parse families validated on it."""
    url = ATHLETE_URL.format(slug=ATHLETE_ANCHOR_SLUG)

    def download() -> str:
        response = ufc_session.get(
            url, timeout=smoke_settings.request_timeout_seconds
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        return response.text

    athlete_html = retry_fetch(f"GET {url}", download)

    # --- enrich_athlete_stats: hero stats block + hero name (homonym guard) ---
    soup = BeautifulSoup(athlete_html, "lxml")
    stats = parse_finish_stats(soup)
    assert stats is not None, (
        f"PARSER BREAK: the athlete page arrived ({len(athlete_html)} chars) but "
        f"parse_finish_stats found no hero-profile__stats block (or no recognized "
        f"label inside it) for {ATHLETE_ANCHOR_SLUG!r}, whose page has carried the "
        f"three career figures for years — the hero stats markup likely changed"
    )
    hero_name = _HERO_NAME_RE.search(athlete_html)
    assert hero_name and hero_name.group(1).strip(), (
        "PARSER BREAK: hero-profile__name not found in the page — every enrich-* "
        "anti-homonym guard would start skipping fighters silently"
    )

    # --- enrich_photos_ufc: W-L-D record + non-placeholder photo styles ---
    record = RECORD_RE.search(athlete_html)
    assert record is not None, (
        f"PARSER BREAK: RECORD_RE found no W-L-D in the {ATHLETE_ANCHOR_SLUG!r} page "
        f"({len(athlete_html)} chars) — the hero-profile__division-body markup likely "
        f"changed (enrich_photos_ufc would stop filling records)"
    )
    wins, losses, draws = (int(group) for group in record.groups())
    assert wins >= 1, (
        f"PARSER BREAK: RECORD_RE matched but reads {wins}-{losses}-{draws} for a "
        f"fighter with a long career — the regex is matching the wrong element"
    )
    candidates: list[str] = []
    for pattern in _IMAGE_RES:
        candidates.extend(pattern.findall(athlete_html))
    real_photos = [c for c in candidates if not _is_placeholder_image(c)]
    assert len(real_photos) >= 1, (
        f"PARSER BREAK: no non-placeholder athlete photo matched _IMAGE_RES "
        f"({len(candidates)} raw candidates) on {ATHLETE_ANCHOR_SLUG!r}'s page — "
        f"UFC likely renamed its image style paths (all photo enrichment would stall)"
    )
