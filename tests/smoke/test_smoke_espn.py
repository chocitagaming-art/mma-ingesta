"""Smoke: ESPN scoreboard + career endpoints still answer the expected shape.

Protects live-results (BE1, 10-min cron on fight night) and espn-history.
All anchors are IMMUTABLE historical facts:

  - scoreboard 2025-06-28 = UFC 317: Topuria vs. Oliveira, a finished card
    whose 11 fights and winner flags can never change;
  - athlete 4275020 = Yaroslav Amosov (the module docstring's own probe id):
    29 already-fought non-UFC (Bellator/regional) bouts that can only grow;
  - athlete 4205093 = Sean O'Malley: his Dana White's Contender Series bout
    (Season 1, Week 2, 2017-07-18, a win), which ESPN files under the UFC
    league — only its event NAME keeps it out of the UFC skip.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.scrapers.espn_fight_history import (
    CONTENDER_SERIES,
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
# Sean O'Malley's DWCS win: "Dana White's Contender Series: Season 1, Week 2".
# ESPN files the whole series under league 3321 (UFC), so parse_career keeps
# it ONLY because is_contender_series recognizes the name: this anchor is the
# alarm for a future ESPN rename of the series.
DWCS_ANCHOR_ESPN_ID = "4205093"
DWCS_ANCHOR_DATE = date(2017, 7, 18)


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


def test_career_keeps_contender_series_bout(espn_session, retry_fetch):
    payload = retry_fetch(
        f"GET career espn_id={DWCS_ANCHOR_ESPN_ID}",
        lambda: fetch_career(espn_session, DWCS_ANCHOR_ESPN_ID),
    )
    assert payload is not None, (
        f"PARSER BREAK: ESPN answered 404 for DWCS anchor athlete "
        f"{DWCS_ANCHOR_ESPN_ID} (Sean O'Malley) — the common/v3 career endpoint "
        f"or its id space changed"
    )
    bouts, counts = parse_career(payload)
    dwcs = [b for b in bouts if b.promotion == CONTENDER_SERIES]
    assert dwcs, (
        f"PARSER BREAK: career payload arrived for {_athlete_page_name(payload)!r} "
        f"but parse_career kept 0 {CONTENDER_SERIES!r} bouts (skip counters: "
        f"{dict(counts)}) — ESPN likely renamed the Dana White's Contender Series "
        f"events and they are being skipped as plain UFC (league 3321) again"
    )
    anchor = [b for b in dwcs if b.event_date == DWCS_ANCHOR_DATE]
    assert anchor and anchor[0].result == "win", (
        f"PARSER BREAK: {len(dwcs)} Contender Series bout(s) parsed but not the "
        f"{DWCS_ANCHOR_DATE} win (Season 1, Week 2): got "
        f"{[(str(b.event_date), b.event_name, b.result) for b in dwcs]}"
    )
