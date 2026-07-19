"""Smoke: ufcstats.com completed-events index + event fight card still parse.

Protects the daily import (main.py / backfills): ufcstats is the authoritative
results source. ALWAYS fetched through UfcStatsClient — it solves the site's
proof-of-work anti-bot challenge; a plain requests.get would only test the
challenge page.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.scrapers.http import UfcStatsClient
from src.scrapers.main import EVENTS_URL as COMPLETED_INDEX_URL
from src.scrapers.parsers.events import parse_events_index
from src.scrapers.parsers.fights import parse_event_fights


pytestmark = pytest.mark.smoke

# The completed index serves 25 events per page (observed 2026-07); it can
# never shrink below a page of history.
MIN_INDEX_EVENTS = 10
# Modern UFC cards have 10+ fights (12 observed on the most recent event);
# even short historical cards carry more than 5.
MIN_EVENT_FIGHTS = 5


@pytest.fixture(scope="module")
def ufcstats_client(smoke_settings) -> UfcStatsClient:
    return UfcStatsClient(smoke_settings)


@pytest.fixture(scope="module")
def index_records(ufcstats_client, smoke_settings, retry_fetch):
    # attempts=1: UfcStatsClient already retries transients AND loops the
    # anti-bot challenge internally; nesting the outer retry would multiply
    # into dozens of requests against a source that is actively blocking.
    page = retry_fetch(
        f"GET {COMPLETED_INDEX_URL}",
        lambda: ufcstats_client.fetch(COMPLETED_INDEX_URL),
        attempts=1,
    )
    return page, parse_events_index(page.soup, smoke_settings)


def test_events_index_parses(index_records):
    page, records = index_records
    assert len(records) >= MIN_INDEX_EVENTS, (
        f"PARSER BREAK: the completed index arrived ({len(page.html)} chars, challenge "
        f"solved) but parse_events_index extracted only {len(records)} events "
        f"(threshold {MIN_INDEX_EVENTS}) — the index table markup likely changed"
    )
    complete = [r for r in records if r.event.name and r.event.event_date]
    assert len(complete) >= MIN_INDEX_EVENTS, (
        f"PARSER BREAK: {len(records)} index rows parsed but only {len(complete)} "
        f"have both name and date (threshold {MIN_INDEX_EVENTS}) — the date span "
        f"(span.b-statistics__date) or link markup likely changed"
    )


def test_event_fights_parse(
    index_records, ufcstats_client, smoke_settings, retry_fetch
):
    _, records = index_records
    # The index's first row can be the NEXT (not yet fought) event; pick the
    # most recent event that has already happened so results/corners exist.
    completed = [
        r for r in records
        if r.event.event_date is not None and r.event.event_date <= date.today()
    ]
    if not completed:
        pytest.skip(
            "index parsed no completed event; test_events_index_parses reports it"
        )
    recent = completed[0]
    detail = retry_fetch(
        f"GET {recent.detail_url}",
        lambda: ufcstats_client.fetch(recent.detail_url),
        attempts=1,  # see index_records: the client already retries internally
    )
    fights = parse_event_fights(detail.soup, smoke_settings)
    assert len(fights) >= MIN_EVENT_FIGHTS, (
        f"PARSER BREAK: event page {recent.detail_url} ({recent.event.name!r}) arrived "
        f"({len(detail.html)} chars) but parse_event_fights extracted only "
        f"{len(fights)} fights (threshold {MIN_EVENT_FIGHTS}) — the tr[data-link] "
        f"card table markup likely changed"
    )
    with_ids = [f for f in fights if f.red_source_id and f.blue_source_id]
    assert len(with_ids) >= MIN_EVENT_FIGHTS, (
        f"PARSER BREAK: {len(fights)} fights parsed but only {len(with_ids)} have "
        f"both corner source_ids (threshold {MIN_EVENT_FIGHTS}) — the "
        f"/fighter-details/ links inside the card rows likely changed"
    )
