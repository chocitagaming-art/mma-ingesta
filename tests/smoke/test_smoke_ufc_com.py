"""Smoke: ufc.com/events listing + event detail bout card still parse.

Protects refresh-upcoming (upcoming_events.py): if UFC redesigns the events
listing or the fight-card markup, production would quietly write 0 events /
0 bouts. Downloads happen once per module (shared session + shared listing).
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from src.scrapers.upcoming_events import (
    EVENTS_URL,
    _get_soup,
    _parse_bouts,
    _parse_listing,
)


pytestmark = pytest.mark.smoke

# ufc.com's listing page 1 shows ~7-8 upcoming cards year-round (7 observed
# 2026-07); UFC always has at least a month of announced events.
MIN_LISTING_EVENTS = 3
# The nearest event always has a full card announced (11 bouts observed
# 2026-07; even early Fight Night cards carry 10+).
MIN_DETAIL_BOUTS = 4


@pytest.fixture(scope="module")
def listing_soup(ufc_session, smoke_settings, retry_fetch) -> BeautifulSoup:
    return retry_fetch(
        f"GET {EVENTS_URL}",
        lambda: _get_soup(ufc_session, EVENTS_URL, smoke_settings),
    )


def test_listing_parses_upcoming_events(listing_soup):
    events = _parse_listing(listing_soup)
    page_chars = len(str(listing_soup))
    assert len(events) >= MIN_LISTING_EVENTS, (
        f"PARSER BREAK: ufc.com/events answered HTTP 200 with a {page_chars}-char page "
        f"but _parse_listing extracted only {len(events)} events "
        f"(threshold {MIN_LISTING_EVENTS}) — listing selectors likely changed"
    )
    complete = [e for e in events if e.source_id and e.event_date]
    assert len(complete) >= MIN_LISTING_EVENTS, (
        f"PARSER BREAK: {len(events)} events parsed but only {len(complete)} have "
        f"both slug and event_date (threshold {MIN_LISTING_EVENTS}) — the date/slug "
        f"markup of the listing cards likely changed"
    )


@pytest.fixture(scope="module")
def first_event_detail(listing_soup, ufc_session, smoke_settings, retry_fetch):
    events = _parse_listing(listing_soup)
    if not events:
        pytest.skip(
            "listing parsed 0 events; test_listing_parses_upcoming_events reports it"
        )
    event = events[0]
    soup = retry_fetch(
        f"GET {event.detail_url}",
        lambda: _get_soup(ufc_session, event.detail_url, smoke_settings),
    )
    return event, soup


def test_event_detail_parses_bouts(first_event_detail):
    event, soup = first_event_detail
    bouts = _parse_bouts(soup, event.source_id)
    page_chars = len(str(soup))
    assert len(bouts) >= MIN_DETAIL_BOUTS, (
        f"PARSER BREAK: event detail {event.detail_url} answered HTTP 200 "
        f"({page_chars} chars) but _parse_bouts extracted only {len(bouts)} bouts "
        f"(threshold {MIN_DETAIL_BOUTS}) — .c-listing-fight markup likely changed"
    )
    named = [
        b for b in bouts
        if b.red_name and b.red_name != "TBD" and b.blue_name and b.blue_name != "TBD"
    ]
    assert len(named) >= MIN_DETAIL_BOUTS, (
        f"PARSER BREAK: {len(bouts)} bouts parsed but only {len(named)} have both "
        f"corner names (threshold {MIN_DETAIL_BOUTS}) — corner-name selectors likely "
        f"changed (parser falls back to 'TBD' when a name node is missing)"
    )
    # Every bout gets a synthetic '<slug>#<order>' fmid as fallback, so a
    # non-trivial check needs at least one REAL data-fmid (production dedups
    # upcoming bouts by it; losing the attribute would corrupt reconciliation).
    real_fmid = [b for b in bouts if b.fmid and "#" not in b.fmid]
    assert len(real_fmid) >= 1, (
        f"PARSER BREAK: none of the {len(bouts)} parsed bouts carries a real data-fmid "
        f"(all fell back to '<slug>#<order>') — the data-fmid attribute likely moved"
    )
