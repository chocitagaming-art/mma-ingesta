"""Fase 5 / BE6: per-section start times from the ufc.com event detail page.

The detail page renders one c-event-fight-card-broadcaster__container per
announced section ("Main Card" / "Prelims" / "Early Prelims"), each carrying
the epoch in __time[data-timestamp] and an ISO instant in <time datetime>
(markup below copied from a real /event/ufc-329 fetch). The parsed hours ride
ParsedEvent.prelims_time / early_prelims_time into upsert_event_meta, whose
new columns follow the never-overwrite-with-NULL policy (COALESCE).

HTML inline + fakedb; no network, no real DB.
"""

from datetime import date, datetime, timezone

from bs4 import BeautifulSoup

from src.scrapers.repositories.events import EventMetaRecord, upsert_event_meta
from src.scrapers.upcoming_events import (
    ParsedEvent,
    _parse_detail,
    _parse_section_times,
)

# Real structure of /event/ufc-329 (2026-07): each section block appears twice
# (desktop + mobile template), so the parser must dedupe by segment. NOTE the
# real page's <time datetime="...Z"> is the ET local hour MISLABELED as Z
# (data-timestamp 1783818000 = 2026-07-12T01:00Z = 9:00 PM EDT): only the
# epoch is trustworthy, and the tests below pin the epoch-derived instants.


def _section_block(title: str, timestamp: str, field_suffix: str, iso: str, label: str) -> str:
    return f"""
<div class="c-event-fight-card-broadcaster__container">
  <div class="c-event-fight-card-broadcaster__desktop-wrapper">
    <div class="c-event-fight-card-broadcaster__card-title">
      <strong>{title}</strong>
    </div>
    <div class="c-event-fight-card-broadcaster__mobile-wrapper">
      <div class="c-event-fight-card-broadcaster__time tz-change-inner" data-locale="en" data-timestamp="{timestamp}" data-format="D, M j / g:i A T">
        <div class="field field--name-fight-card-time-{field_suffix} field--type-datetime field--label-hidden field__item"><time datetime="{iso}">{label}</time>
        </div>
      </div>
      <div class="c-event-fight-card-broadcaster__name"></div>
    </div>
  </div>
</div>
"""


MAIN_BLOCK = _section_block("Main Card", "1783818000", "main", "2026-07-11T21:00:00Z", "Sat, Jul 11 / 9:00 PM")
PRELIMS_BLOCK = _section_block("Prelims", "1783810800", "prelims", "2026-07-11T19:00:00Z", "Sat, Jul 11 / 7:00 PM")
EARLY_BLOCK = _section_block("Early Prelims", "1783803600", "early", "2026-07-11T17:00:00Z", "Sat, Jul 11 / 5:00 PM")

# Desktop + mobile duplicates, like the real page.
EVENT_HTML = f"""
<html><head><meta property="og:title" content="UFC 329 | UFC"></head><body>
{MAIN_BLOCK}{PRELIMS_BLOCK}{EARLY_BLOCK}
{MAIN_BLOCK}{PRELIMS_BLOCK}{EARLY_BLOCK}
</body></html>
"""

# Epoch-derived UTC instants (9:00 PM / 7:00 PM / 5:00 PM EDT on July 11).
MAIN_TIME = datetime(2026, 7, 12, 1, 0, tzinfo=timezone.utc)
PRELIMS_TIME = datetime(2026, 7, 11, 23, 0, tzinfo=timezone.utc)
EARLY_TIME = datetime(2026, 7, 11, 21, 0, tzinfo=timezone.utc)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _event(**overrides) -> ParsedEvent:
    defaults = dict(
        source_id="ufc-329",
        detail_url="https://www.ufc.com/event/ufc-329",
        headliner="McGregor vs Holloway",
        event_date=date(2026, 7, 11),
        start_time=MAIN_TIME,
        location=None,
        ticket_url=None,
    )
    defaults.update(overrides)
    return ParsedEvent(**defaults)


# ------------------------------------------------------------------- parsing


def test_parse_section_times_reads_all_three_sections():
    times = _parse_section_times(_soup(EVENT_HTML))
    assert times == {"main": MAIN_TIME, "prelims": PRELIMS_TIME, "early_prelims": EARLY_TIME}


def test_parse_section_times_never_trusts_the_mislabeled_time_datetime():
    # Without a numeric epoch the section is OMITTED: the inner <time
    # datetime="...Z"> is ET mislabeled as UTC and would shift hours by 4-5h.
    html = EVENT_HTML.replace('data-timestamp="1783810800"', 'data-timestamp=""')
    times = _parse_section_times(_soup(html))
    assert "prelims" not in times
    assert times["main"] == MAIN_TIME


def test_parse_section_times_ignores_unannounced_sections():
    # Far-out cards only announce the main card (often not even that).
    times = _parse_section_times(_soup(f"<html><body>{MAIN_BLOCK}</body></html>"))
    assert times == {"main": MAIN_TIME}
    assert _parse_section_times(_soup("<html><body></body></html>")) == {}


def test_parse_detail_fills_section_times():
    event = _event()
    _parse_detail(_soup(EVENT_HTML), event)
    assert event.prelims_time == PRELIMS_TIME
    assert event.early_prelims_time == EARLY_TIME
    # The listing's main-card start_time is authoritative and untouched.
    assert event.start_time == MAIN_TIME


def test_parse_detail_backfills_missing_start_time_from_main_block():
    event = _event(start_time=None)
    _parse_detail(_soup(EVENT_HTML), event)
    assert event.start_time == MAIN_TIME


def test_parsed_event_defaults_keep_old_constructions_valid():
    event = _event()
    assert event.prelims_time is None
    assert event.early_prelims_time is None


# --------------------------------------------------------------- persistence


def _meta_record(**overrides) -> EventMetaRecord:
    defaults = dict(
        name="UFC 329: McGregor vs. Holloway 2",
        event_date=date(2026, 7, 11),
        start_time=MAIN_TIME,
        location="Las Vegas, Nevada",
        promotion_id=1,
        status="upcoming",
        image_url=None,
        tagline=None,
        broadcast="PPV",
        ticket_url=None,
        headliner="McGregor vs Holloway",
        source="ufc.com",
        source_id="ufc-329",
        prelims_time=PRELIMS_TIME,
        early_prelims_time=EARLY_TIME,
    )
    defaults.update(overrides)
    return EventMetaRecord(**defaults)


def test_update_branch_coalesces_section_times(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(7,)])
    event_id = upsert_event_meta(conn, _meta_record())
    assert event_id == 7
    sql, params = conn.cursors[0].executed[1]
    flat = " ".join(sql.split())
    # Never overwrite a stored hour with NULL (article/announcement lag).
    assert "prelims_time = COALESCE(%s, prelims_time)" in flat
    assert "early_prelims_time = COALESCE(%s, early_prelims_time)" in flat
    assert params[-3:] == (PRELIMS_TIME, EARLY_TIME, 7)


def test_update_branch_binds_null_for_unannounced_sections(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(7,)])
    upsert_event_meta(conn, _meta_record(prelims_time=None, early_prelims_time=None))
    _sql, params = conn.cursors[0].executed[1]
    # NULL through the COALESCE = keep the stored value.
    assert params[-3:] == (None, None, 7)


def test_insert_branch_carries_and_coalesces_section_times(fakedb):
    def responder(sql, params=None):
        if "WHERE source = %s" in sql:
            return []  # not found by (source, source_id) -> INSERT path
        return [(9,)]

    conn = fakedb.Connection(responder)
    event_id = upsert_event_meta(conn, _meta_record())
    assert event_id == 9
    sql, params = conn.cursors[0].executed[1]
    flat = " ".join(sql.split())
    assert "prelims_time, early_prelims_time" in flat
    assert "prelims_time = COALESCE(EXCLUDED.prelims_time, events.prelims_time)" in flat
    assert (
        "early_prelims_time = COALESCE(EXCLUDED.early_prelims_time, events.early_prelims_time)"
        in flat
    )
    assert params[-2:] == (PRELIMS_TIME, EARLY_TIME)


def test_event_meta_record_defaults_keep_old_constructions_valid():
    record = _meta_record()
    trimmed = EventMetaRecord(
        name=record.name, event_date=record.event_date, start_time=None,
        location=None, promotion_id=1, status="upcoming", image_url=None,
        tagline=None, broadcast=None, ticket_url=None, headliner=None,
        source="ufc.com", source_id="ufc-329",
    )
    assert trimmed.prelims_time is None
    assert trimmed.early_prelims_time is None
