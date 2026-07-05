"""Fase 5 / BE8: referee + judges' scorecards from ufcstats fight-details.

Markup copied from real 2026-06 fight-details fetches: the header paragraph
labels "Referee:" inside a b-fight-details__text-item, and (decisions only)
the "Details:" paragraph carries one label-less text-item per judge
("<span>Ben Cartlidge</span> 28 - 29."). Finishes keep free text there, so
only the referee is parsed. Score orientation is re-derived per fight from
the page's person blocks vs the DB corners (source ids -> fuzzy names ->
positional default).

HTML inline + fakedb; no network, no real DB.
"""

from collections import Counter
from types import SimpleNamespace

from bs4 import BeautifulSoup

from src.scrapers.fight_officials import (
    ParsedScorecard,
    TargetFight,
    _process_fight,
    first_person_is_red,
    insert_scorecard,
    list_target_fights,
    parse_fight_officials,
    resolve_first_person_is_red,
    update_fight_referee,
)

# --------------------------------------------------------------------- HTML


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


def _details_page(persons: str, method: str, details_items: str) -> str:
    return f"""
<html><body>
{persons}
<div class="b-fight-details__fight">
  <div class="b-fight-details__content">
    <p class="b-fight-details__text">
      <i class="b-fight-details__text-item_first">
        <i class="b-fight-details__label">Method:</i>
        <i style="font-style: normal"> {method} </i>
      </i>
      <i class="b-fight-details__text-item">
        <i class="b-fight-details__label">Round:</i>
        3
      </i>
      <i class="b-fight-details__text-item">
        <i class="b-fight-details__label">Time:</i>
        5:00
      </i>
      <i class="b-fight-details__text-item">
        <i class="b-fight-details__label">Time format:</i>
        3 Rnd (5-5-5)
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
      {details_items}
    </p>
  </div>
</div>
</body></html>
"""


JUDGE_ITEMS = """
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
"""

PERSONS = _persons_block(
    "http://ufcstats.com/fighter-details/06734ca9d88dec3a", "Shara Magomedov",
    "http://ufcstats.com/fighter-details/595db60957de51d3", "Michel Pereira",
)

DECISION_HTML = _details_page(PERSONS, "Decision - Unanimous", JUDGE_ITEMS)
KO_HTML = _details_page(PERSONS, "KO/TKO", "Punches to Head On Ground")


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _fight(**overrides) -> TargetFight:
    defaults = dict(
        fight_id=11,
        source_id="/fight-details/012c307c9d446c4d",
        red_source_id="/fighter-details/06734ca9d88dec3a",
        blue_source_id="/fighter-details/595db60957de51d3",
        red_name="Shara Magomedov",
        blue_name="Michel Pereira",
    )
    defaults.update(overrides)
    return TargetFight(**defaults)


# ------------------------------------------------------------------- parsing


def test_parse_decision_page_reads_referee_and_three_scorecards():
    officials = parse_fight_officials(_soup(DECISION_HTML))
    assert officials.referee == "Herb Dean"
    assert officials.scorecards == [
        ParsedScorecard("Ben Cartlidge", 28, 29),
        ParsedScorecard("Anders Ohlsson", 28, 29),
        ParsedScorecard("Vito Paolillo", 27, 30),
    ]
    assert officials.persons == [
        ("/fighter-details/06734ca9d88dec3a", "Shara Magomedov"),
        ("/fighter-details/595db60957de51d3", "Michel Pereira"),
    ]


def test_parse_finish_page_reads_referee_only():
    officials = parse_fight_officials(_soup(KO_HTML))
    assert officials.referee == "Herb Dean"
    # "Punches to Head On Ground" is free text, not a judge item.
    assert officials.scorecards == []


def test_parse_page_without_referee_span():
    html = DECISION_HTML.replace('<span class="">\n            Herb Dean\n        </span>', "")
    officials = parse_fight_officials(_soup(html))
    assert officials.referee is None


# ------------------------------------------------------------------ mapping


def test_first_person_is_red_by_source_id():
    officials = parse_fight_officials(_soup(DECISION_HTML))
    assert first_person_is_red(officials, _fight()) is True
    # DB row with swapped corners: the page's first person is the DB blue.
    swapped = _fight(
        red_source_id="/fighter-details/595db60957de51d3",
        blue_source_id="/fighter-details/06734ca9d88dec3a",
        red_name="Michel Pereira",
        blue_name="Shara Magomedov",
    )
    assert first_person_is_red(officials, swapped) is False


def test_first_person_is_red_falls_back_to_names_then_positional():
    officials = parse_fight_officials(_soup(DECISION_HTML))
    # No source ids stored (historical rows) -> fuzzy names at 0.92.
    by_name = _fight(red_source_id=None, blue_source_id=None,
                     red_name="Michel Pereira", blue_name="Shara Magomedov")
    assert first_person_is_red(officials, by_name) is False
    # Nothing matches -> positional default (importer lists red first).
    unknown = _fight(red_source_id=None, blue_source_id=None,
                     red_name="Someone Else", blue_name="Another Person")
    assert first_person_is_red(officials, unknown) is True


def test_resolve_first_person_is_red_returns_none_when_unresolvable():
    # Strict variant for callers without the ufcstats page-order guarantee
    # (backfill_results' ufc.com bouts): no match -> None, never a guess.
    officials = parse_fight_officials(_soup(DECISION_HTML))
    unknown = _fight(red_source_id=None, blue_source_id=None,
                     red_name="Someone Else", blue_name="Another Person")
    assert resolve_first_person_is_red(officials, unknown) is None
    assert resolve_first_person_is_red(officials, _fight()) is True


# --------------------------------------------------------------------- write


def test_update_fight_referee_never_stomps_stored_value(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    update_fight_referee(conn, 11, "Herb Dean")
    sql, params = conn.cursors[0].executed[0]
    assert "referee = COALESCE(referee, %s)" in " ".join(sql.split())
    assert params == ("Herb Dean", 11)


def test_insert_scorecard_on_conflict_do_nothing(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    assert insert_scorecard(conn, 11, "Ben Cartlidge", 28, 29) is True
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "INSERT INTO fight_scorecards" in flat
    assert "ON CONFLICT (fight_id, judge_name) DO NOTHING" in flat
    assert params == (11, "Ben Cartlidge", 28, 29)


# ------------------------------------------------------------------ targets


def test_list_target_fights_filters_and_limit(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    list_target_fights(conn, 30, 50)
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "f.source = 'ufcstats'" in flat
    assert "f.referee IS NULL" in flat
    assert "(f.winner_id IS NOT NULL OR f.method IS NOT NULL)" in flat
    assert "e.event_date >= CURRENT_DATE - %s" in flat
    assert flat.endswith("LIMIT %s")
    assert params == (30, 50)


# ------------------------------------------------------------------ pipeline


class _FakeClient:
    def __init__(self, html: str):
        self._html = html
        self.fetched: list[str] = []

    def fetch(self, url: str):
        self.fetched.append(url)
        return SimpleNamespace(url=url, html=self._html, soup=_soup(self._html))


def test_process_fight_writes_referee_and_oriented_scorecards(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    client = _FakeClient(DECISION_HTML)
    counts: Counter = Counter()
    # DB corners swapped vs the page order -> scores must swap too.
    fight = _fight(
        red_source_id="/fighter-details/595db60957de51d3",
        blue_source_id="/fighter-details/06734ca9d88dec3a",
        red_name="Michel Pereira",
        blue_name="Shara Magomedov",
    )
    _process_fight(conn, client, counts, fight, dry_run=False)

    assert client.fetched == ["http://ufcstats.com/fight-details/012c307c9d446c4d"]
    statements = [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
    ]
    referee_updates = [s for s in statements if "SET referee" in s[0]]
    assert [p for _s, p in referee_updates] == [("Herb Dean", 11)]
    scorecard_inserts = [p for s, p in statements if "INSERT INTO fight_scorecards" in s]
    assert scorecard_inserts == [
        (11, "Ben Cartlidge", 29, 28),
        (11, "Anders Ohlsson", 29, 28),
        (11, "Vito Paolillo", 30, 27),
    ]
    assert counts["referees_parsed"] == 1
    assert counts["scorecards_inserted"] == 3


def test_process_fight_finish_writes_referee_only(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    counts: Counter = Counter()
    _process_fight(conn, _FakeClient(KO_HTML), counts, _fight(), dry_run=False)
    mutating = fakedb.mutating_statements(conn)
    assert len(mutating) == 1 and "SET referee" in mutating[0]
    assert counts["scorecards_parsed"] == 0


def test_process_fight_dry_run_never_writes(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    counts: Counter = Counter()
    _process_fight(conn, _FakeClient(DECISION_HTML), counts, _fight(), dry_run=True)
    assert fakedb.mutating_statements(conn) == []
    assert counts["referees_parsed"] == 1
    assert counts["scorecards_parsed"] == 3
