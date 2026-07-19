"""Smoke: ESPN scoreboard + career endpoints still answer the expected shape.

Protects live-results (BE1, 10-min cron on fight night) and espn-history.
Both anchors are IMMUTABLE historical facts:

  - scoreboard 2025-06-28 = UFC 317: Topuria vs. Oliveira, a finished card
    whose 11 fights and winner flags can never change;
  - athlete 4275020 = Yaroslav Amosov (the module docstring's own probe id):
    29 already-fought non-UFC (Bellator/regional) bouts that can only grow.
"""

from __future__ import annotations

import pytest

from src.scrapers.espn_fight_history import (
    _athlete_page_name,
    fetch_career,
    parse_career,
)
from src.scrapers.espn_live_results import fetch_scoreboard, parse_scoreboard


pytestmark = pytest.mark.smoke

# UFC 317: Topuria vs. Oliveira — completed card, results frozen forever.
SCOREBOARD_ANCHOR_DATE = "20250628"
# Yaroslav Amosov: 29 non-UFC bouts already on record (2026-07); asserting >=1
# keeps the threshold lax while still catching "parsed nothing".
CAREER_ANCHOR_ESPN_ID = "4275020"
VALID_RESULTS = {"win", "loss", "draw", "nc"}


def test_scoreboard_parses_historical_card(espn_session, retry_fetch):
    payload = retry_fetch(
        f"GET scoreboard dates={SCOREBOARD_ANCHOR_DATE}",
        lambda: fetch_scoreboard(espn_session, SCOREBOARD_ANCHOR_DATE),
    )
    events = parse_scoreboard(payload)
    raw_events = len(payload.get("events", []) or [])
    assert len(events) >= 1, (
        f"PARSER BREAK: the scoreboard JSON arrived ({raw_events} raw events) "
        f"but parse_scoreboard returned 0 events for {SCOREBOARD_ANCHOR_DATE} "
        f"(UFC 317, a finished card) — the events/competitions shape likely changed"
    )
    fights = [fight for event in events for fight in event.fights]
    named = [f for f in fights if f.red_name and f.blue_name]
    assert len(named) >= 1, (
        f"PARSER BREAK: {len(events)} scoreboard events parsed but no fight has "
        f"both corner names — the competitors/athlete.displayName shape likely changed"
    )
    assert any(f.winner_espn_id for f in fights), (
        f"PARSER BREAK: {len(fights)} fights parsed for UFC 317 (all decided long ago) "
        f"but none has a winner flagged — the competitors[].winner shape likely changed"
    )


def test_career_parses_anchor_athlete(espn_session, retry_fetch):
    payload = retry_fetch(
        f"GET career espn_id={CAREER_ANCHOR_ESPN_ID}",
        lambda: fetch_career(espn_session, CAREER_ANCHOR_ESPN_ID),
    )
    # fetch_career maps a 404 to None: for this long-established anchor that
    # means the endpoint moved or the id space changed, not a network blip.
    assert payload is not None, (
        f"PARSER BREAK: ESPN answered 404 for anchor athlete {CAREER_ANCHOR_ESPN_ID} "
        f"(Yaroslav Amosov) — the common/v3 career endpoint or its id space changed"
    )
    athlete_name = _athlete_page_name(payload)
    assert athlete_name, (
        "PARSER BREAK: career payload arrived but athlete.displayName is empty — "
        "the identity guard of espn_fight_history would skip every import"
    )
    bouts, counts = parse_career(payload)
    assert len(bouts) >= 1, (
        f"PARSER BREAK: career payload arrived for {athlete_name!r} "
        f"({len(payload.get('eventsMap') or {})} events in ESPN, skip counters: "
        f"{dict(counts)}) but parse_career produced 0 non-UFC bouts — the "
        f"eventsMap/uid/gameResult shape likely changed"
    )
    bad_results = [b.result for b in bouts if b.result not in VALID_RESULTS]
    assert not bad_results, (
        f"PARSER BREAK: parse_career emitted results outside {sorted(VALID_RESULTS)}: "
        f"{sorted(set(bad_results))}"
    )
