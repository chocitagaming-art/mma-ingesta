"""backfill_event_fights: re-import the full ufcstats card of an EXISTING
completed event (the importers skip events already present, so a card that
landed incomplete — e.g. event 315 with 2 of ~12 fights — was unrepairable).

Covers the index walk (exact date + name-token overlap, paginated newest-first),
the upsert keying (source='ufcstats' + fight-details source_id onto the EXISTING
event_id, so present fights update and missing ones insert), and that --dry-run
parses + reports without writing. HTML inline + fakedb; no network, no real DB.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from src.scrapers.backfill_event_fights import (
    _find_ufcstats_event_url,
    _flip_event_completed,
    _get_events_needing_fights,
    _sweep,
    backfill_event_fights,
)
from src.scrapers.config import Settings

SETTINGS = Settings(database_url="postgres://fake/db", anthropic_api_key=None)

EVENT_ID = 315
EVENT_NAME = "UFC Fight Night: Imavov vs. Borralho"
EVENT_DATE = date(2025, 9, 6)
EVENT_ROW = (EVENT_NAME, EVENT_DATE, "completed")

INDEX_URL_1 = "http://ufcstats.com/statistics/events/completed?page=1"
INDEX_URL_2 = "http://ufcstats.com/statistics/events/completed?page=2"
EVENT_URL = "http://ufcstats.com/event-details/e315abc"
FIGHT1_URL = "http://ufcstats.com/fight-details/f0001"
FIGHT2_URL = "http://ufcstats.com/fight-details/f0002"

RED1_HREF = "http://ufcstats.com/fighter-details/aaa1"
BLUE1_HREF = "http://ufcstats.com/fighter-details/bbb2"
RED2_HREF = "http://ufcstats.com/fighter-details/ccc3"
BLUE2_HREF = "http://ufcstats.com/fighter-details/ddd4"

FIGHTER_IDS = {
    "/fighter-details/aaa1": 201,
    "/fighter-details/bbb2": 202,
    "/fighter-details/ccc3": 203,
    "/fighter-details/ddd4": 204,
}

# --------------------------------------------------------------------- HTML


def _index_html(rows) -> str:
    """Completed-events index page: (detail_url, name, 'Month DD, YYYY') rows."""
    trs = "".join(
        '<tr class="b-statistics__table-row">'
        '<td class="b-statistics__table-col"><i class="b-statistics__table-content">'
        f'<a href="{url}">{name}</a>'
        f'<span class="b-statistics__date">{date_text}</span>'
        "</i></td>"
        '<td class="b-statistics__table-col">Paris, France</td></tr>'
        for url, name, date_text in rows
    )
    return f"<html><body><table><tbody>{trs}</tbody></table></body></html>"


# Page 1 only carries events NEWER than the target -> the walk must continue.
PAGE1_HTML = _index_html(
    [
        ("http://ufcstats.com/event-details/newer1", "UFC 999: New vs. Guy", "October 04, 2025"),
        ("http://ufcstats.com/event-details/newer2", "UFC Fight Night: Also vs. Newer", "September 20, 2025"),
    ]
)
# Page 2 has TWO same-date events: the name-token overlap must pick the right one.
PAGE2_HTML = _index_html(
    [
        (EVENT_URL, EVENT_NAME, "September 06, 2025"),
        ("http://ufcstats.com/event-details/decoy", "UFC Fight Night: Somebody vs. Else", "September 06, 2025"),
        ("http://ufcstats.com/event-details/older1", "UFC 998: Old vs. Card", "August 16, 2025"),
    ]
)
# A page already PAST the target date -> the event is not listed, stop walking.
PAST_PAGE_HTML = _index_html(
    [("http://ufcstats.com/event-details/older1", "UFC 998: Old vs. Card", "August 16, 2025")]
)


def _event_fight_row(fight_url, red_href, red_name, blue_href, blue_name, winner="red") -> str:
    wl = "<p>W</p><p>L</p>" if winner == "red" else "<p>L</p><p>W</p>"
    return f"""
<tr data-link="{fight_url}">
  <td>{wl}</td>
  <td><p><a href="{red_href}">{red_name}</a></p>
      <p><a href="{blue_href}">{blue_name}</a></p></td>
  <td><p>25</p><p>22</p></td>
  <td><p>2</p><p>0</p></td>
  <td><p>0</p><p>0</p></td>
  <td><p>0</p><p>0</p></td>
  <td><p>Middleweight Bout</p></td>
  <td><p>KO/TKO</p><p>Punches</p></td>
  <td><p>2</p></td>
  <td><p>0:15</p></td>
</tr>"""


EVENT_HTML = f"""
<html><body><table><tbody>
{_event_fight_row(FIGHT1_URL, RED1_HREF, "Nassourdine Imavov", BLUE1_HREF, "Caio Borralho", winner="red")}
{_event_fight_row(FIGHT2_URL, RED2_HREF, "Prelim Red", BLUE2_HREF, "Prelim Blue", winner="blue")}
</tbody></table></body></html>
"""


def _fight_html(red_href, red_name, blue_href, blue_name, winner="red") -> str:
    red_status, blue_status = ("W", "L") if winner == "red" else ("L", "W")
    head = "".join(
        f'<th class="b-fight-details__table-col">{th}</th>'
        for th in ("Fighter", "KD", "Sig. str.", "Sig. str. %", "Total str.",
                   "Td", "Td %", "Sub. att", "Rev.", "Ctrl")
    )
    return f"""
<html><body>
<div class="b-fight-details__persons clearfix">
  <div class="b-fight-details__person">
    <i class="b-fight-details__person-status">{red_status}</i>
    <h3 class="b-fight-details__person-name"><a href="{red_href}">{red_name}</a></h3>
  </div>
  <div class="b-fight-details__person">
    <i class="b-fight-details__person-status">{blue_status}</i>
    <h3 class="b-fight-details__person-name"><a href="{blue_href}">{blue_name}</a></h3>
  </div>
</div>
<table class="b-fight-details__table">
<thead><tr>{head}</tr></thead>
<tbody>
<tr>
  <td><p><a href="{red_href}">{red_name}</a></p><p><a href="{blue_href}">{blue_name}</a></p></td>
  <td><p>1</p><p>0</p></td>
  <td><p>25 of 39</p><p>22 of 52</p></td>
  <td><p>60%</p><p>41%</p></td>
  <td><p>28 of 43</p><p>26 of 56</p></td>
  <td><p>2 of 2</p><p>0 of 0</p></td>
  <td><p>---</p><p>---</p></td>
  <td><p>0</p><p>0</p></td>
  <td><p>0</p><p>0</p></td>
  <td><p>0:42</p><p>0:06</p></td>
</tr>
</tbody></table>
</body></html>
"""


ALL_PAGES = {
    INDEX_URL_1: PAGE1_HTML,
    INDEX_URL_2: PAGE2_HTML,
    EVENT_URL: EVENT_HTML,
    FIGHT1_URL: _fight_html(RED1_HREF, "Nassourdine Imavov", BLUE1_HREF, "Caio Borralho", winner="red"),
    FIGHT2_URL: _fight_html(RED2_HREF, "Prelim Red", BLUE2_HREF, "Prelim Blue", winner="blue"),
}


class _FakeClient:
    def __init__(self, pages: dict[str, str]):
        self._pages = pages
        self.fetched: list[str] = []

    def fetch(self, url: str):
        from bs4 import BeautifulSoup

        self.fetched.append(url)
        html = self._pages[url]
        return SimpleNamespace(url=url, html=html, soup=BeautifulSoup(html, "lxml"))


def make_responder(event_rows=None, fighter_ids=FIGHTER_IDS, fight_ids=(3332, 4001)):
    event_rows = {EVENT_ID: EVENT_ROW} if event_rows is None else event_rows
    pending_fight_ids = list(fight_ids)

    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT name, event_date, status FROM events"):
            row = event_rows.get(params[0])
            return [row] if row else []
        if "FROM fighters WHERE source = %s AND source_id = %s" in flat:
            fighter_id = fighter_ids.get(params[1])
            return [(fighter_id,)] if fighter_id else []
        if "INSERT INTO fights" in flat:
            return [(pending_fight_ids.pop(0),)]
        return []

    return responder


def _statements(conn):
    return [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
    ]


# ------------------------------------------------------------- event loading


def test_rejects_missing_event(fakedb):
    conn = fakedb.Connection(make_responder())
    with pytest.raises(ValueError, match="not found"):
        backfill_event_fights(conn, _FakeClient({}), SETTINGS, 999)


def test_rejects_non_completed_event(fakedb):
    rows = {316: ("UFC 999: New vs. Guy", date(2026, 1, 1), "upcoming")}
    conn = fakedb.Connection(make_responder(event_rows=rows))
    with pytest.raises(ValueError, match="upcoming"):
        backfill_event_fights(conn, _FakeClient({}), SETTINGS, 316)


# ------------------------------------------------------------ index matching


def test_find_event_walks_pages_and_matches_date_plus_name():
    client = _FakeClient({INDEX_URL_1: PAGE1_HTML, INDEX_URL_2: PAGE2_HTML})
    url = _find_ufcstats_event_url(client, SETTINGS, EVENT_NAME, EVENT_DATE)
    # Two events share the date on page 2: the token overlap picks the right
    # one, and page 1 (all newer) forced the walk to continue.
    assert url == EVENT_URL
    assert client.fetched == [INDEX_URL_1, INDEX_URL_2]


def test_find_event_stops_once_the_index_is_past_the_date():
    # The first page is already older than the target -> the event is not
    # listed; stop without fetching further pages (page 2 would KeyError).
    client = _FakeClient({INDEX_URL_1: PAST_PAGE_HTML})
    assert _find_ufcstats_event_url(client, SETTINGS, EVENT_NAME, EVENT_DATE) is None
    assert client.fetched == [INDEX_URL_1]


# ------------------------------------------------------------------- backfill


def test_backfill_upserts_fights_keyed_by_ufcstats_source(fakedb):
    conn = fakedb.Connection(make_responder())
    client = _FakeClient(ALL_PAGES)
    counts = backfill_event_fights(conn, client, SETTINGS, EVENT_ID)

    assert counts["event_matched"] == 1
    assert counts["fights_parsed"] == 2
    assert counts["fights_upserted"] == 2
    assert counts["stats_rows"] == 4  # 2 fighters x 2 fights

    fight_upserts = [(s, p) for s, p in _statements(conn) if "INSERT INTO fights" in s]
    assert len(fight_upserts) == 2
    for flat, _params in fight_upserts:
        # UNIQUE (source, source_id) + DO UPDATE: the 2 pre-existing fights are
        # updated in place, the missing ones inserted -> zero duplicates.
        assert "ON CONFLICT (source, source_id)" in flat
        assert "DO UPDATE" in flat
    # (event_id, ..., winner_id, ..., is_title_fight, source, source_id, is_title_fight):
    # always the EXISTING event row, keyed by the ufcstats fight-details path.
    # is_title_fight is bound at [-3] (insert) and re-bound at [-1] (conflict
    # COALESCE), so source/source_id are now at [-3]/[-2] counting from that pair.
    assert [(p[0], p[6], p[-3], p[-2]) for _s, p in fight_upserts] == [
        (EVENT_ID, 201, "ufcstats", "/fight-details/f0001"),
        (EVENT_ID, 204, "ufcstats", "/fight-details/f0002"),
    ]

    stats_upserts = [p for s, p in _statements(conn) if "INSERT INTO fight_stats" in s]
    assert [p[:2] for p in stats_upserts] == [
        (3332, 201), (3332, 202), (4001, 203), (4001, 204),
    ]
    assert conn.commits == 1  # single commit once the whole event is in


def test_backfill_dry_run_parses_and_reports_without_writing(fakedb):
    conn = fakedb.Connection(make_responder())
    client = _FakeClient(ALL_PAGES)
    counts = backfill_event_fights(conn, client, SETTINGS, EVENT_ID, dry_run=True)

    # The report still says what WOULD be written...
    assert counts["fights_parsed"] == 2
    assert counts["fights_upserted"] == 2
    assert counts["stats_rows"] == 4
    # ... but nothing is.
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_backfill_dry_run_skips_fights_with_unknown_fighters(fakedb):
    # Fight 2's fighters are absent from the DB. In --dry-run fighters are only
    # RESOLVED (creating them writes), so that fight is skipped and counted.
    known = {k: v for k, v in FIGHTER_IDS.items() if v in (201, 202)}
    conn = fakedb.Connection(make_responder(fighter_ids=known))
    client = _FakeClient(ALL_PAGES)
    counts = backfill_event_fights(conn, client, SETTINGS, EVENT_ID, dry_run=True)

    assert counts["fights_upserted"] == 1
    assert counts["fights_skipped_missing_fighter"] == 1
    assert fakedb.mutating_statements(conn) == []


# --------------------------------------------- guard against foreign-source cards


def _responder_with_foreign_sources(foreign_by_event, event_rows=None):
    """Responder where the guard query returns foreign sources for some events."""
    event_rows = {EVENT_ID: EVENT_ROW} if event_rows is None else event_rows

    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT name, event_date, status FROM events"):
            row = event_rows.get(params[0])
            return [row] if row else []
        if flat.startswith("SELECT DISTINCT source FROM fights"):
            return [(s,) for s in foreign_by_event.get(params[0], [])]
        return []

    return responder


def test_guard_rejects_event_with_ufc_com_card_before_fetching(fakedb):
    # Event 315 already carries a ufc.com card: re-importing from ufcstats would
    # duplicate every bout (rows of another source never conflict-update). The
    # guard must fire BEFORE any HTTP fetch so --dry-run surfaces it too.
    conn = fakedb.Connection(_responder_with_foreign_sources({EVENT_ID: ["ufc.com"]}))
    client = _FakeClient({})  # any fetch would KeyError -> proves nothing was fetched
    with pytest.raises(ValueError, match="duplicate"):
        backfill_event_fights(conn, client, SETTINGS, EVENT_ID)
    assert client.fetched == []
    assert fakedb.mutating_statements(conn) == []


def test_guard_allows_event_with_only_its_own_source(fakedb):
    # The event's existing 2 fights are already ufcstats -> no foreign source,
    # the guard passes and the backfill proceeds normally (full success path).
    base = make_responder()

    def responder(sql, params=None):
        if " ".join(sql.split()).startswith("SELECT DISTINCT source FROM fights"):
            return [("ufcstats",)]
        return base(sql, params)

    conn = fakedb.Connection(responder)
    client = _FakeClient(ALL_PAGES)
    counts = backfill_event_fights(conn, client, SETTINGS, EVENT_ID)
    assert counts["fights_upserted"] == 2


# ----------------------------------------------------------- candidate query


def test_get_events_needing_fights_query_shape_and_limit(fakedb):
    rows = [
        (315, "UFC Fight Night: Imavov vs. Borralho", EVENT_DATE, "completed"),
        (400, "UFC 998: Old vs. Card", date(2025, 8, 16), "completed"),
        (401, "Stuck Past Event", date(2025, 7, 1), "upcoming"),
    ]
    conn = fakedb.Connection(lambda sql, params=None: rows)
    got = _get_events_needing_fights(conn, "ufcstats", limit=2, max_fights=6)
    assert got == rows[:2]  # limit applied
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    # Past events only, excluding any event with a foreign-source fight, whose
    # card is below the threshold. Ordered newest-first.
    assert "e.event_date < CURRENT_DATE" in flat
    assert "f.source IS DISTINCT FROM %s" in flat
    assert "count(*) FROM fights f WHERE f.event_id = e.id) < %s" in flat
    assert "ORDER BY e.event_date DESC" in flat
    assert params == ("ufcstats", 6)


# ------------------------------------------------------------ status flip


def test_flip_event_completed_is_guarded_by_past_date(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])  # rowcount 1
    assert _flip_event_completed(conn, 401) is True
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "UPDATE events SET status = 'completed'" in flat
    assert "status <> 'completed' AND event_date < CURRENT_DATE" in flat
    assert params == (401,)


# ------------------------------------------------------------- sweep driver


def test_sweep_continues_after_a_failing_event(fakedb):
    # Event 900 trips the foreign-source guard (fails cleanly, no fetch); the
    # sweep must count the error and STILL backfill event 315.
    event_rows = {900: ("UFC Foreign: A vs. B", date(2025, 1, 1), "completed"), EVENT_ID: EVENT_ROW}
    base = _responder_with_foreign_sources({900: ["ufc.com"]}, event_rows=event_rows)
    fight_ids = list((3332, 4001))

    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if "FROM fighters WHERE source = %s AND source_id = %s" in flat:
            return [(FIGHTER_IDS.get(params[1]),)] if FIGHTER_IDS.get(params[1]) else []
        if "INSERT INTO fights" in flat:
            return [(fight_ids.pop(0),)]
        return base(sql, params)

    conn = fakedb.Connection(responder)
    client = _FakeClient(ALL_PAGES)
    candidates = [
        (900, "UFC Foreign: A vs. B", date(2025, 1, 1), "completed"),
        (EVENT_ID, EVENT_NAME, EVENT_DATE, "completed"),
    ]
    totals = _sweep(conn, client, SETTINGS, candidates, dry_run=False)

    assert totals["events_candidate"] == 2
    assert totals["event_errors"] == 1  # event 900 (foreign card)
    assert totals["events_done"] == 1  # event 315 still backfilled
    assert totals["fights_upserted"] == 2


def test_sweep_dry_run_skips_upcoming_without_writing(fakedb):
    # A past event still stuck in 'upcoming': in --dry-run we cannot flip (that
    # writes), so it is counted and skipped, never fetched.
    conn = fakedb.Connection(lambda sql, params=None: [])
    client = _FakeClient({})
    candidates = [(401, "Stuck Past Event", date(2025, 7, 1), "upcoming")]
    totals = _sweep(conn, client, SETTINGS, candidates, dry_run=True)

    assert totals["events_upcoming_skipped_dry_run"] == 1
    assert totals["events_done"] == 0
    assert client.fetched == []
    assert fakedb.mutating_statements(conn) == []


def test_sweep_flips_upcoming_then_backfills(fakedb):
    # Non-dry: a past 'upcoming' event is flipped to 'completed' (committed),
    # then backfilled. The responder models the flip so _get_event then sees
    # 'completed' (the fake DB is otherwise static).
    flipped: set[int] = set()
    fight_ids = list((3332, 4001))

    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("UPDATE events SET status = 'completed'"):
            flipped.add(params[0])
            return [(1,)]  # rowcount 1
        if flat.startswith("SELECT name, event_date, status FROM events"):
            status = "completed" if params[0] in flipped else "upcoming"
            return [(EVENT_NAME, EVENT_DATE, status)]
        if flat.startswith("SELECT DISTINCT source FROM fights"):
            return []
        if "FROM fighters WHERE source = %s AND source_id = %s" in flat:
            return [(FIGHTER_IDS.get(params[1]),)] if FIGHTER_IDS.get(params[1]) else []
        if "INSERT INTO fights" in flat:
            return [(fight_ids.pop(0),)]
        return []

    conn = fakedb.Connection(responder)
    client = _FakeClient(ALL_PAGES)
    candidates = [(EVENT_ID, EVENT_NAME, EVENT_DATE, "upcoming")]
    totals = _sweep(conn, client, SETTINGS, candidates, dry_run=False)

    assert totals["events_status_flipped"] == 1
    assert totals["events_done"] == 1
    assert totals["fights_upserted"] == 2
    flip_stmts = [
        s for s, _p in _statements(conn) if s.startswith("UPDATE events SET status = 'completed'")
    ]
    assert len(flip_stmts) == 1
