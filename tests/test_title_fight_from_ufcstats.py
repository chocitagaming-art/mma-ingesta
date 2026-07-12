"""Title-fight capture from ufcstats (belt icon) — parser + upsert + backfill.

The results scraper never recorded whether a FOUGHT bout was for a title, so
every ufcstats fight sat at the migration-010 default FALSE (only the ufc.com
UPCOMING scraper set the flag, on different rows). ufcstats marks a title bout
with a belt icon (``<img src=".../belt.png">``) in the card row's weight-class
cell — the cell TEXT stays the plain division. These tests lock:

  1. parse_event_fights reads the belt icon (and ignores bonus icons).
  2. upsert_fight persists is_title_fight with the no-NULL-overwrite policy.
  3. set_fight_title_flag / title_flag_would_change guard the backfill UPDATE.
  4. sweep_title_fights walks the index -> events -> per-fight flag update.

HTML inline + fakedb; no network, no real DB.
"""

from bs4 import BeautifulSoup

from src.scrapers.backfill_title_fights import sweep_title_fights
from src.scrapers.config import Settings
from src.scrapers.models import FightRecord
from src.scrapers.parsers.fights import parse_event_fights
from src.scrapers.repositories.fights import (
    set_fight_title_flag,
    title_flag_would_change,
    upsert_fight,
)

SETTINGS = Settings(database_url="postgres://fake/db", anthropic_api_key=None)

BELT = "http://cdn.example/belt.png"
PERF = "http://cdn.example/perf.png"  # Performance of the Night bonus (NOT a title)
FIGHT_TITLE = "http://ufcstats.com/fight-details/tt111"
FIGHT_PLAIN = "http://ufcstats.com/fight-details/pp222"
FIGHT_BONUS = "http://ufcstats.com/fight-details/bb333"


def _row(fight_url: str, weight_cell_html: str) -> str:
    return f"""
<tr data-link="{fight_url}">
  <td><p>W</p><p>L</p></td>
  <td><p><a href="http://ufcstats.com/fighter-details/r1">Red Fighter</a></p>
      <p><a href="http://ufcstats.com/fighter-details/b1">Blue Fighter</a></p></td>
  <td><p>25</p><p>22</p></td>
  <td><p>2</p><p>0</p></td>
  <td><p>0</p><p>0</p></td>
  <td><p>0</p><p>0</p></td>
  <td>{weight_cell_html}</td>
  <td><p>KO/TKO</p><p>Punches</p></td>
  <td><p>2</p></td>
  <td><p>0:15</p></td>
</tr>"""


EVENT_HTML = f"""
<html><body><table><tbody>
{_row(FIGHT_TITLE, f'<img src="{BELT}"><p>Featherweight</p>')}
{_row(FIGHT_PLAIN, '<p>Bantamweight</p>')}
{_row(FIGHT_BONUS, f'<img src="{PERF}"><p>Lightweight</p>')}
</tbody></table></body></html>
"""


# ------------------------------------------------------------ 1) parser

def test_parse_event_fights_reads_the_belt_icon():
    fights = {f.source_id: f for f in parse_event_fights(BeautifulSoup(EVENT_HTML, "lxml"), SETTINGS)}
    assert fights["/fight-details/tt111"].is_title_fight is True   # belt.png
    assert fights["/fight-details/pp222"].is_title_fight is False  # no icon
    assert fights["/fight-details/bb333"].is_title_fight is False  # perf.png bonus, not a belt
    # The belt does NOT bleed into the visible weight class (text stays clean).
    assert fights["/fight-details/tt111"].weight_class == "Featherweight"


# ------------------------------------------------------------ 2) upsert_fight

def _fight(is_title):
    return FightRecord(
        event_id=7, fighter_red_id=1, fighter_blue_id=2, weight_class="Featherweight",
        weight_grams=None, scheduled_rounds=5, winner_id=1, method="KO/TKO",
        end_round=3, end_time="4:00", odds_red=None, odds_blue=None,
        source="ufcstats", source_id="/fight-details/tt111", is_title_fight=is_title,
    )


def test_upsert_fight_persists_title_true(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(55,)])
    assert upsert_fight(conn, _fight(True)) == 55
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "is_title_fight" in flat
    assert "COALESCE(%s, FALSE)" in flat  # NOT NULL column, insert side
    assert "is_title_fight = COALESCE(%s, fights.is_title_fight)" in flat  # conflict side
    # Bound on the insert (13th value) and re-bound for the conflict update (last).
    assert params[12] is True
    assert params[-1] is True


def test_upsert_fight_null_title_never_overwrites(fakedb):
    # Default is_title_fight=None ("unknown"): insert falls back to FALSE, and the
    # conflict branch keeps the stored value (raw None re-bound, not coalesced-FALSE).
    conn = fakedb.Connection(lambda sql, params=None: [(55,)])
    upsert_fight(conn, _fight(None))
    _sql, params = conn.cursors[0].executed[0]
    assert params[12] is None
    assert params[-1] is None


# ------------------------------------------------ 3) backfill repo helpers

def test_set_fight_title_flag_guarded_update(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])  # one row changed
    assert set_fight_title_flag(conn, "ufcstats", "/fight-details/tt111", True) is True
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert flat.startswith("UPDATE fights")
    assert "is_title_fight IS DISTINCT FROM %s" in flat  # only writes on real change
    assert params == (True, "ufcstats", "/fight-details/tt111", True)


def test_set_fight_title_flag_no_change_returns_false(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])  # rowcount 0
    assert set_fight_title_flag(conn, "ufcstats", "/fight-details/pp222", False) is False


def test_title_flag_would_change_is_read_only(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert title_flag_would_change(conn, "ufcstats", "/fight-details/tt111", True) is True
    sql, _params = conn.cursors[0].executed[0]
    assert sql.strip().upper().startswith("SELECT")  # never writes


# ------------------------------------------------------------ 4) sweep

INDEX_URL_1 = "http://ufcstats.com/statistics/events/completed?page=1"
INDEX_URL_2 = "http://ufcstats.com/statistics/events/completed?page=2"
EVENT_URL = "http://ufcstats.com/event-details/e001"

INDEX_HTML = f"""
<html><body><table><tbody>
<tr class="b-statistics__table-row">
  <td class="b-statistics__table-col"><i class="b-statistics__table-content">
    <a href="{EVENT_URL}">UFC 245: Usman vs. Covington</a>
    <span class="b-statistics__date">December 14, 2019</span></i></td>
  <td class="b-statistics__table-col">Las Vegas, USA</td>
</tr>
</tbody></table></body></html>
"""

PAGES = {INDEX_URL_1: INDEX_HTML, INDEX_URL_2: "<html><body></body></html>", EVENT_URL: EVENT_HTML}


class _FakeClient:
    def __init__(self, pages):
        self._pages = pages
        self.fetched = []

    def fetch(self, url):
        self.fetched.append(url)
        return type("Page", (), {"soup": BeautifulSoup(self._pages[url], "lxml")})()


def test_sweep_updates_only_the_title_fight(fakedb):
    # The DB responder: the title fight's row flips (rowcount 1), the others are
    # already correct (rowcount 0). Keyed by the source_id bound in the UPDATE.
    def responder(sql, params=None):
        if params and "/fight-details/tt111" in params:
            return [(1,)]
        return []

    conn = fakedb.Connection(responder)
    counts = sweep_title_fights(conn, _FakeClient(PAGES), SETTINGS, dry_run=False)

    assert counts["events_total"] == 1
    assert counts["events_done"] == 1
    assert counts["fights_seen"] == 3
    assert counts["title_fights_on_card"] == 1  # only the belt row
    assert counts["rows_changed"] == 1
    assert counts["rows_set_true"] == 1
    assert conn.commits == 1  # committed once, after the event


def test_sweep_dry_run_never_writes(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)] if (params and "/fight-details/tt111" in params) else [])
    counts = sweep_title_fights(conn, _FakeClient(PAGES), SETTINGS, dry_run=True)
    assert counts["rows_changed"] == 1  # would-change still counted
    assert conn.commits == 0
    # Every statement the sweep ran against the DB is a read.
    for sql in fakedb.executed_statements(conn):
        assert sql.strip().upper().startswith("SELECT")
