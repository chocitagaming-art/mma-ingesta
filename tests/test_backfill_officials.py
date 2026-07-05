"""Officials (referee + scorecards) for ufc.com-sourced bouts via backfill_results.

fight_officials only targets fights.source='ufcstats', so bouts scraped from
ufc.com (source_id=fmid) would keep referee NULL and no fight_scorecards
forever. backfill_results already opens each bout's ufcstats fight-details
page to fill results/stats; these tests cover the added step that fills the
officials off that SAME soup: candidate query gap, officials-only bouts
(referee missing, or referee filled but a decision without its scorecards),
scorecard orientation (normal, swapped, unresolvable -> skip + counter),
dry-run and second-pass idempotence.

HTML inline + fakedb; no network, no real DB.
"""

from collections import Counter
from types import SimpleNamespace

from bs4 import BeautifulSoup

from src.scrapers.backfill_results import (
    _Bout,
    _fill_event,
    _fill_officials,
    _get_bouts,
    _get_events_needing_results,
    _is_decision,
)

# --------------------------------------------------------------------- HTML

EVENT_URL = "http://ufcstats.com/event-details/31e1ea6fe6b682f8"
FIGHT_URL = "http://ufcstats.com/fight-details/012c307c9d446c4d"
RED_HREF = "http://ufcstats.com/fighter-details/06734ca9d88dec3a"
BLUE_HREF = "http://ufcstats.com/fighter-details/595db60957de51d3"

# Event page row (parse_event_fights): decision result, no stats needed here.
EVENT_HTML = f"""
<html><body><table><tbody>
<tr data-link="{FIGHT_URL}">
  <td><p>W</p><p>L</p></td>
  <td><p><a href="{RED_HREF}">Shara Magomedov</a></p>
      <p><a href="{BLUE_HREF}">Michel Pereira</a></p></td>
  <td><p>25</p><p>22</p></td>
  <td><p>2</p><p>0</p></td>
  <td><p>0</p><p>0</p></td>
  <td><p>0</p><p>0</p></td>
  <td><p>Middleweight Bout</p></td>
  <td><p>U-DEC</p></td>
  <td><p>3</p></td>
  <td><p>5:00</p></td>
</tr>
</tbody></table></body></html>
"""


def _persons_block(first_href: str, first_name: str, second_href: str, second_name: str) -> str:
    return f"""
<div class="b-fight-details__persons clearfix">
  <div class="b-fight-details__person">
    <i class="b-fight-details__person-status b-fight-details__person-status_style_green">W</i>
    <div class="b-fight-details__person-text">
      <h3 class="b-fight-details__person-name">
        <a class='b-link b-fight-details__person-link' href={first_href}>{first_name} </a>
      </h3>
    </div>
  </div>
  <div class="b-fight-details__person">
    <i class="b-fight-details__person-status b-fight-details__person-status_style_gray">L</i>
    <div class="b-fight-details__person-text">
      <h3 class="b-fight-details__person-name">
        <a class='b-link b-fight-details__person-link' href={second_href}>{second_name} </a>
      </h3>
    </div>
  </div>
</div>
"""


def _details_page(persons: str) -> str:
    return f"""
<html><body>
{persons}
<div class="b-fight-details__fight">
  <div class="b-fight-details__content">
    <p class="b-fight-details__text">
      <i class="b-fight-details__text-item_first">
        <i class="b-fight-details__label">Method:</i>
        <i style="font-style: normal"> Decision - Unanimous </i>
      </i>
      <i class="b-fight-details__text-item">
        <i class="b-fight-details__label">Referee:</i>
        <span class="">
            Herb Dean
        </span>
      </i>
    </p>
    <p class="b-fight-details__text">
      <i class="b-fight-details__text-item_first">
        <i class="b-fight-details__label">Details:</i>
      </i>
      <i class="b-fight-details__text-item">
        <span>Ben Cartlidge</span>
        28 - 29.
      </i>
      <i class="b-fight-details__text-item">
        <span>Anders Ohlsson</span>
        28 - 29.
      </i>
      <i class="b-fight-details__text-item">
        <span>Vito Paolillo</span>
        27 - 30.
      </i>
    </p>
  </div>
</div>
</body></html>
"""


FIGHT_HTML = _details_page(_persons_block(RED_HREF, "Shara Magomedov", BLUE_HREF, "Michel Pereira"))
# Same fight, but the detail page spells the names in a way that matches
# NEITHER corner (and carries no usable source ids for the bout's fighters).
UNRESOLVABLE_HTML = _details_page(_persons_block(
    "http://ufcstats.com/fighter-details/000000000000000a", "Fighter One",
    "http://ufcstats.com/fighter-details/000000000000000b", "Fighter Two",
))


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _bout(**overrides) -> _Bout:
    # ufc.com bout already result-complete: only the officials are missing.
    defaults = dict(
        id=11, red_id=201, blue_id=202,
        red_name="Shara Magomedov", blue_name="Michel Pereira",
        method="Decision - Unanimous", has_stats=True, has_round_stats=True,
        referee=None, has_scorecards=False,
        red_source_id="/fighter-details/06734ca9d88dec3a",
        blue_source_id="/fighter-details/595db60957de51d3",
    )
    defaults.update(overrides)
    return _Bout(**defaults)


class _FakeClient:
    def __init__(self, pages: dict[str, str]):
        self._pages = pages
        self.fetched: list[str] = []

    def fetch(self, url: str):
        self.fetched.append(url)
        html = self._pages[url]
        return SimpleNamespace(url=url, html=html, soup=_soup(html))


def _statements(conn) -> list[tuple[str, object]]:
    return [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
    ]


# ---------------------------------------------------------------- candidates


def test_events_qualify_when_result_filled_but_referee_missing(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    _get_events_needing_results(conn, None)
    sql, _params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    # New branch: an event with everything filled EXCEPT officials re-qualifies.
    assert "fi.referee IS NULL" in flat
    assert "(fi.winner_id IS NOT NULL OR fi.method IS NOT NULL)" in flat
    # Existing criteria (results/stats) stay untouched.
    assert "fi.method IS NULL" in flat
    assert "CURRENT_DATE - INTERVAL '60 days'" in flat


def test_get_bouts_reads_officials_state(fakedb):
    row = (
        11, 201, 202, "Shara Magomedov", "Michel Pereira", "Decision - Unanimous",
        True, True, None, False,
        "/fighter-details/06734ca9d88dec3a", "/fighter-details/595db60957de51d3",
    )
    conn = fakedb.Connection(lambda sql, params=None: [row])
    bout = _get_bouts(conn, 5)[0]
    assert bout.referee is None
    assert bout.has_scorecards is False
    assert bout.red_source_id == "/fighter-details/06734ca9d88dec3a"
    assert bout.blue_source_id == "/fighter-details/595db60957de51d3"
    sql, _params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "fi.referee" in flat
    assert "EXISTS (SELECT 1 FROM fight_scorecards sc WHERE sc.fight_id = fi.id)" in flat


def test_is_decision_covers_all_spellings():
    assert _is_decision("U-DEC") and _is_decision("Decision - Unanimous") and _is_decision("Decision")
    assert not _is_decision("KO/TKO - Punches") and not _is_decision("Submission") and not _is_decision(None)


# ------------------------------------------------------------------ pipeline


def test_fill_event_fills_officials_only_bout(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    client = _FakeClient({EVENT_URL: EVENT_HTML, FIGHT_URL: FIGHT_HTML})
    counts: Counter = Counter()
    # Result + stats already filled: the bout enters the loop just for officials.
    _fill_event(conn, client, None, 5, [_bout()], EVENT_URL, counts, False)
    assert client.fetched == [EVENT_URL, FIGHT_URL]
    statements = _statements(conn)
    referee_updates = [p for s, p in statements if "SET referee" in s]
    assert referee_updates == [("Herb Dean", 11)]
    scorecard_inserts = [p for s, p in statements if "INSERT INTO fight_scorecards" in s]
    assert scorecard_inserts == [
        (11, "Ben Cartlidge", 28, 29),
        (11, "Anders Ohlsson", 28, 29),
        (11, "Vito Paolillo", 27, 30),
    ]
    # The already-real result is never rewritten.
    assert not any("winner_id" in s for s, _p in statements)
    assert counts["referees_filled"] == 1
    assert counts["scorecards_inserted"] == 3
    assert counts["officials_unresolved"] == 0


def test_fill_event_admits_decision_bout_missing_only_scorecards(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    client = _FakeClient({EVENT_URL: EVENT_HTML, FIGHT_URL: FIGHT_HTML})
    counts: Counter = Counter()
    # Result, stats AND referee already filled; only the judges' scorecards
    # are missing. The bout must still enter the loop — solely through the
    # second needs_officials() branch (decision without scorecards) — e.g. on
    # an event that re-qualified because of ANOTHER bout.
    bout = _bout(referee="Herb Dean")
    _fill_event(conn, client, None, 5, [bout], EVENT_URL, counts, False)
    # The fight page IS opened just for the scorecards.
    assert client.fetched == [EVENT_URL, FIGHT_URL]
    statements = _statements(conn)
    # Referee already stored -> never rewritten; result untouched too.
    assert not any("SET referee" in s for s, _p in statements)
    assert not any("winner_id" in s for s, _p in statements)
    scorecard_inserts = [p for s, p in statements if "INSERT INTO fight_scorecards" in s]
    assert scorecard_inserts == [
        (11, "Ben Cartlidge", 28, 29),
        (11, "Anders Ohlsson", 28, 29),
        (11, "Vito Paolillo", 27, 30),
    ]
    assert counts["referees_filled"] == 0
    assert counts["scorecards_inserted"] == 3


def test_fill_event_fills_result_and_officials_from_one_fetch(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    client = _FakeClient({EVENT_URL: EVENT_HTML, FIGHT_URL: FIGHT_HTML})
    counts: Counter = Counter()
    bouts = [_bout(method=None)]  # fresh past event: no result yet either
    _fill_event(conn, client, None, 5, bouts, EVENT_URL, counts, False)
    # One fight-page fetch serves result AND officials.
    assert client.fetched == [EVENT_URL, FIGHT_URL]
    statements = _statements(conn)
    result_updates = [p for s, p in statements if "winner_id = COALESCE(%s, winner_id)" in s]
    assert len(result_updates) == 1
    assert result_updates[0][0] == 201        # W on the page's first person = red
    assert result_updates[0][1] == "U-DEC"    # method from the event page row
    assert [p for s, p in statements if "SET referee" in s] == [("Herb Dean", 11)]
    assert len([p for s, p in statements if "INSERT INTO fight_scorecards" in s]) == 3
    assert counts["bouts_filled"] == 1
    assert counts["referees_filled"] == 1
    assert counts["scorecards_inserted"] == 3


def test_fill_event_orients_scorecards_for_swapped_corners(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    client = _FakeClient({EVENT_URL: EVENT_HTML, FIGHT_URL: FIGHT_HTML})
    counts: Counter = Counter()
    # DB corners swapped vs the page order, no source ids (ufc.com fighters):
    # orientation falls back to fuzzy names and the scores must swap.
    swapped = _bout(
        red_name="Michel Pereira", blue_name="Shara Magomedov",
        red_source_id=None, blue_source_id=None,
    )
    _fill_event(conn, client, None, 5, [swapped], EVENT_URL, counts, False)
    scorecard_inserts = [p for s, p in _statements(conn) if "INSERT INTO fight_scorecards" in s]
    assert scorecard_inserts == [
        (11, "Ben Cartlidge", 29, 28),
        (11, "Anders Ohlsson", 29, 28),
        (11, "Vito Paolillo", 30, 27),
    ]
    assert counts["scorecards_inserted"] == 3


def test_fill_event_skips_scorecards_when_orientation_unresolvable(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    client = _FakeClient({EVENT_URL: EVENT_HTML, FIGHT_URL: UNRESOLVABLE_HTML})
    counts: Counter = Counter()
    # Detail-page names match neither corner and the source ids are unknown:
    # the referee (corner-agnostic) still lands, the scorecards never guess.
    bout = _bout(red_source_id=None, blue_source_id=None)
    _fill_event(conn, client, None, 5, [bout], EVENT_URL, counts, False)
    statements = _statements(conn)
    assert [p for s, p in statements if "SET referee" in s] == [("Herb Dean", 11)]
    assert not any("INSERT INTO fight_scorecards" in s for s, _p in statements)
    assert counts["referees_filled"] == 1
    assert counts["officials_unresolved"] == 1
    assert counts["scorecards_inserted"] == 0


def test_fill_event_second_pass_never_refetches_or_rewrites(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    client = _FakeClient({EVENT_URL: EVENT_HTML, FIGHT_URL: FIGHT_HTML})
    counts: Counter = Counter()
    # Everything (result, stats, referee, scorecards) already in the DB.
    done = _bout(referee="Herb Dean", has_scorecards=True)
    _fill_event(conn, client, None, 5, [done], EVENT_URL, counts, False)
    assert client.fetched == [EVENT_URL]  # the fight page is not even opened
    assert fakedb.mutating_statements(conn) == []
    assert counts["referees_filled"] == 0
    assert counts["scorecards_inserted"] == 0


def test_fill_event_officials_dry_run_counts_but_never_writes(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    client = _FakeClient({EVENT_URL: EVENT_HTML, FIGHT_URL: FIGHT_HTML})
    counts: Counter = Counter()
    _fill_event(conn, client, None, 5, [_bout()], EVENT_URL, counts, True)
    assert fakedb.mutating_statements(conn) == []
    assert counts["referees_filled"] == 1
    assert counts["scorecards_inserted"] == 3


def test_fill_officials_fills_only_whats_missing(fakedb):
    soup = _soup(FIGHT_HTML)
    # Referee already stored -> only the scorecards are written.
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    counts: Counter = Counter()
    _fill_officials(conn, _bout(referee="Marc Goddard"), soup, counts, False)
    mutating = fakedb.mutating_statements(conn)
    assert len(mutating) == 3 and all("fight_scorecards" in s for s in mutating)
    assert counts["referees_filled"] == 0
    # Scorecards already stored -> only the referee is written.
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    counts = Counter()
    _fill_officials(conn, _bout(has_scorecards=True), soup, counts, False)
    mutating = fakedb.mutating_statements(conn)
    assert len(mutating) == 1 and "SET referee" in mutating[0]
    assert counts["scorecards_inserted"] == 0


def test_fill_officials_counts_pages_without_officials(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    counts: Counter = Counter()
    _fill_officials(conn, _bout(), _soup("<html><body></body></html>"), counts, False)
    assert fakedb.mutating_statements(conn) == []
    assert counts["officials_missing"] == 1
