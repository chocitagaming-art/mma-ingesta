"""Fase 6 / BE4: stats por asalto (fight_stats_rounds, migración 012).

Markup copiado de un fetch real de fight-details (2026-07, Fiziev vs Torres):
la tabla "Per round" (class b-fight-details__table) intercala dentro del tbody
un marcador <thead class="...__table-row_type_head"><th>Round N</th></thead>
por asalto con la fila de datos; las tablas de TOTALES no llevan class y no
entran en el selector. parse_fight_stats_rounds persiste cada fila además del
total que parse_fight_stats ya suma.

HTML inline + fakedb; sin red y sin BD real.
"""

from collections import Counter
from types import SimpleNamespace

from bs4 import BeautifulSoup

from src.scrapers.backfill_results import _Bout, _fill_event, _fill_round_stats
from src.scrapers.backfill_round_stats import (
    _process_fight as process_round_backfill_fight,
    list_target_fights,
)
from src.scrapers.models import FightStatsRoundRecord
from src.scrapers.parsers.fights import (
    build_fight_stats_round_record,
    parse_fight_stats_rounds,
)
from src.scrapers.repositories.fights import upsert_fight_stats_rounds

# --------------------------------------------------------------------- HTML

RED_HREF = "http://ufcstats.com/fighter-details/c814b4c899793af6"
BLUE_HREF = "http://ufcstats.com/fighter-details/2e7878927067fdca"


def _fighter_cell() -> str:
    return f"""
  <td class="b-fight-details__table-col l-page_align_left">
    <p class="b-fight-details__table-text">
      <a class='b-link b-link_style_black' href={RED_HREF}>Rafael Fiziev </a>
    </p>
    <p class="b-fight-details__table-text">
      <a class='b-link b-link_style_black' href={BLUE_HREF}>Manuel Torres </a>
    </p>
  </td>
"""


def _dual(red: str, blue: str) -> str:
    return (
        f'<td class="b-fight-details__table-col">'
        f'<p class="b-fight-details__table-text">{red}</p>'
        f'<p class="b-fight-details__table-text">{blue}</p></td>'
    )


def _round_marker(number: int) -> str:
    return (
        '<thead class="b-fight-details__table-row b-fight-details__table-row_type_head">'
        f'<th class="b-fight-details__table-col" colspan="10">Round {number}</th>'
        "</thead>"
    )


def _round_row(kd, sig, total, td, sub, rev, ctrl) -> str:
    return f"""
<tr class="b-fight-details__table-row">
{_fighter_cell()}
{_dual(*kd)}
{_dual(*sig)}
{_dual("60%", "41%")}
{_dual(*total)}
{_dual(*td)}
{_dual("---", "---")}
{_dual(*sub)}
{_dual(*rev)}
{_dual(*ctrl)}
</tr>
"""


_HEAD = (
    '<thead class="b-fight-details__table-head_rnd"><tr class="b-fight-details__table-row">'
    + "".join(
        f'<th class="b-fight-details__table-col">{th}</th>'
        for th in ("Fighter", "KD", "Sig. str.", "Sig. str. %", "Total str.",
                   "Td", "Td %", "Sub. att", "Rev.", "Ctrl")
    )
    + "</tr></thead>"
)

# Tabla de TOTALES sin class (real: solo las "Per round" llevan
# b-fight-details__table) -> el selector no debe verla nunca.
_TOTALS_TABLE = f"""
<table>{_HEAD}<tbody>
{_round_row(("1", "0"), ("25 of 39", "22 of 52"), ("28 of 43", "26 of 56"),
            ("2 of 2", "0 of 0"), ("0", "0"), ("0", "0"), ("0:42", "0:06"))}
</tbody></table>
"""

ROUNDS_HTML = f"""
<html><body>
{_TOTALS_TABLE}
<table class="b-fight-details__table js-fight-table">
{_HEAD}
<tbody class="b-fight-details__table-body">
{_round_marker(1)}
{_round_row(("0", "0"), ("15 of 25", "21 of 51"), ("18 of 29", "25 of 55"),
            ("2 of 2", "0 of 0"), ("0", "0"), ("0", "0"), ("0:36", "0:06"))}
{_round_marker(2)}
{_round_row(("1", "0"), ("10 of 14", "1 of 1"), ("10 of 14", "1 of 1"),
            ("0 of 0", "0 of 0"), ("0", "0"), ("0", "0"), ("0:06", "0:00"))}
</tbody>
</table>
</body></html>
"""

# Markup antiguo: la tabla con class es un único total SIN marcadores Round N.
TOTALS_ONLY_HTML = f"""
<html><body>
<table class="b-fight-details__table">{_HEAD}<tbody>
{_round_row(("1", "0"), ("25 of 39", "22 of 52"), ("28 of 43", "26 of 56"),
            ("2 of 2", "0 of 0"), ("0", "0"), ("0", "0"), ("0:42", "0:06"))}
</tbody></table>
</body></html>
"""


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# ------------------------------------------------------------------- parser


def test_parse_fight_stats_rounds_reads_each_round_and_fighter():
    rounds = parse_fight_stats_rounds(_soup(ROUNDS_HTML))
    assert [(r.round, r.fighter_name) for r in rounds] == [
        (1, "Rafael Fiziev"), (1, "Manuel Torres"),
        (2, "Rafael Fiziev"), (2, "Manuel Torres"),
    ]
    fiziev_r1, torres_r1, fiziev_r2, torres_r2 = rounds
    assert fiziev_r1.fighter_source_id == "/fighter-details/c814b4c899793af6"
    assert fiziev_r1.stats == {
        "sig_strikes_landed": 15, "sig_strikes_attempted": 25,
        "takedowns_landed": 2, "takedowns_attempted": 2,
        "submission_attempts": 0, "control_time_seconds": 36, "knockdowns": 0,
    }
    assert torres_r1.stats["sig_strikes_landed"] == 21
    assert torres_r1.stats["sig_strikes_attempted"] == 51
    assert torres_r1.stats["control_time_seconds"] == 6
    assert fiziev_r2.stats["knockdowns"] == 1
    assert fiziev_r2.stats["sig_strikes_landed"] == 10
    assert torres_r2.stats["control_time_seconds"] == 0
    # Los rounds suman exactamente el total que guarda fight_stats.
    assert fiziev_r1.stats["sig_strikes_landed"] + fiziev_r2.stats["sig_strikes_landed"] == 25


def test_parse_fight_stats_rounds_ignores_totals_only_markup():
    # Tabla con class pero sin marcadores "Round N" = tabla de totales antigua:
    # tratar su única fila como "round 1" seria inventar datos.
    assert parse_fight_stats_rounds(_soup(TOTALS_ONLY_HTML)) == []
    assert parse_fight_stats_rounds(_soup("<html><body></body></html>")) == []


def test_build_fight_stats_round_record_maps_all_columns():
    parsed = parse_fight_stats_rounds(_soup(ROUNDS_HTML))[0]
    record = build_fight_stats_round_record(11, 201, parsed)
    assert record == FightStatsRoundRecord(
        fight_id=11, fighter_id=201, round=1,
        sig_strikes_landed=15, sig_strikes_attempted=25,
        takedowns_landed=2, takedowns_attempted=2,
        submission_attempts=0, control_time_seconds=36, knockdowns=0,
    )


# -------------------------------------------------------------------- write


def test_upsert_fight_stats_rounds_on_conflict_coalesce(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    record = FightStatsRoundRecord(11, 201, 2, 10, 14, 0, 0, 0, 6, 1)
    upsert_fight_stats_rounds(conn, record)
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "INSERT INTO fight_stats_rounds" in flat
    assert "ON CONFLICT (fight_id, fighter_id, round)" in flat
    # Un re-scrape con NULL nunca borra un valor almacenado.
    assert "sig_strikes_landed = COALESCE(EXCLUDED.sig_strikes_landed, fight_stats_rounds.sig_strikes_landed)" in flat
    assert "knockdowns = COALESCE(EXCLUDED.knockdowns, fight_stats_rounds.knockdowns)" in flat
    assert params == (11, 201, 2, 10, 14, 0, 0, 0, 6, 1)


# ----------------------------------------- integración cron diario (backfill_results)


def _bout(**overrides) -> _Bout:
    defaults = dict(
        id=11, red_id=201, blue_id=202,
        red_name="Rafael Fiziev", blue_name="Manuel Torres",
        method="U-DEC", has_stats=True, has_round_stats=False,
    )
    defaults.update(overrides)
    return _Bout(**defaults)


def test_fill_round_stats_writes_one_row_per_fighter_round(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    counts: Counter = Counter()
    _fill_round_stats(conn, _bout(), _soup(ROUNDS_HTML), counts, dry_run=False)
    inserts = [
        params
        for cur in conn.cursors
        for sql, params in cur.executed
        if "INSERT INTO fight_stats_rounds" in sql
    ]
    # (fight_id, fighter_id, round, ...) para 2 luchadores x 2 rounds,
    # mapeados a las esquinas del bout por nombre.
    assert [p[:3] for p in inserts] == [
        (11, 201, 1), (11, 202, 1), (11, 201, 2), (11, 202, 2),
    ]
    assert counts["round_stats_rows"] == 4


def test_fill_round_stats_dry_run_and_unmatched(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    counts: Counter = Counter()
    _fill_round_stats(conn, _bout(), _soup(ROUNDS_HTML), counts, dry_run=True)
    assert fakedb.mutating_statements(conn) == []
    assert counts["round_stats_rows"] == 4
    # Nombres que no casan con el bout -> se cuentan, no se escriben.
    counts = Counter()
    stranger = _bout(red_name="Somebody Else", blue_name="Another Person")
    _fill_round_stats(conn, stranger, _soup(ROUNDS_HTML), counts, dry_run=False)
    assert counts["round_stats_unmatched"] == 4


class _FakeClient:
    def __init__(self, pages: dict[str, str]):
        self._pages = pages
        self.fetched: list[str] = []

    def fetch(self, url: str):
        self.fetched.append(url)
        html = self._pages[url]
        return SimpleNamespace(url=url, html=html, soup=_soup(html))


EVENT_URL = "http://ufcstats.com/event-details/31e1ea6fe6b682f8"
FIGHT_URL = "http://ufcstats.com/fight-details/c13dc0cccef263f7"

EVENT_HTML = f"""
<html><body><table><tbody>
<tr data-link="{FIGHT_URL}">
  <td><p>W</p><p>L</p></td>
  <td><p><a href="{RED_HREF}">Rafael Fiziev</a></p>
      <p><a href="{BLUE_HREF}">Manuel Torres</a></p></td>
  <td><p>25</p><p>22</p></td>
  <td><p>2</p><p>0</p></td>
  <td><p>0</p><p>0</p></td>
  <td><p>0</p><p>0</p></td>
  <td><p>Lightweight Bout</p></td>
  <td><p>KO/TKO</p><p>Punches</p></td>
  <td><p>2</p></td>
  <td><p>0:15</p></td>
</tr>
</tbody></table></body></html>
"""

PERSONS_HTML = f"""
<div class="b-fight-details__persons clearfix">
  <div class="b-fight-details__person">
    <i class="b-fight-details__person-status">W</i>
    <h3 class="b-fight-details__person-name"><a href="{RED_HREF}">Rafael Fiziev</a></h3>
  </div>
  <div class="b-fight-details__person">
    <i class="b-fight-details__person-status">L</i>
    <h3 class="b-fight-details__person-name"><a href="{BLUE_HREF}">Manuel Torres</a></h3>
  </div>
</div>
"""

FIGHT_HTML = ROUNDS_HTML.replace("<html><body>", f"<html><body>{PERSONS_HTML}")


def test_fill_event_persists_round_rows_for_new_fights(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    client = _FakeClient({EVENT_URL: EVENT_HTML, FIGHT_URL: FIGHT_HTML})
    counts: Counter = Counter()
    bouts = [_bout()]  # ya tiene resultado ufcstats y totales; faltan rounds
    _fill_event(conn, client, None, 5, bouts, EVENT_URL, counts, False)
    assert client.fetched == [EVENT_URL, FIGHT_URL]
    statements = [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
    ]
    round_inserts = [p for s, p in statements if "INSERT INTO fight_stats_rounds" in s]
    assert [p[:3] for p in round_inserts] == [
        (11, 201, 1), (11, 202, 1), (11, 201, 2), (11, 202, 2),
    ]
    # El método 'U-DEC' es de ufcstats: el resultado NO se reescribe.
    assert not any(s.startswith("UPDATE fights") for s, _p in statements)


def test_fill_event_upgrades_provisional_espn_method(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    client = _FakeClient({EVENT_URL: EVENT_HTML, FIGHT_URL: FIGHT_HTML})
    counts: Counter = Counter()
    # La madrugada anterior el updater de ESPN dejó el código provisional:
    # ufcstats lo mejora ('KO/TKO - Punches') sin poder degradar el winner.
    bouts = [_bout(method="KO/TKO", has_round_stats=True)]
    _fill_event(conn, client, None, 5, bouts, EVENT_URL, counts, False)
    updates = [
        (" ".join(sql.split()), params)
        for cur in conn.cursors
        for sql, params in cur.executed
        if sql.strip().startswith("UPDATE fights")
    ]
    assert len(updates) == 1
    flat, params = updates[0]
    assert "winner_id = COALESCE(%s, winner_id)" in flat
    assert "(method IS NULL OR method = ANY(%s))" in flat
    assert params[0] == 201                     # ganador por nombre (W en red)
    assert params[1] == "KO/TKO - Punches"      # método detallado de ufcstats
    assert counts["bouts_filled"] == 1


def test_fill_event_reprocesa_un_combate_con_el_desglose_a_medias(fakedb):
    """El bout 14895 (UFC 330): 3 asaltos disputados, solo el R1 guardado.

    Tiene resultado de ufcstats, sus 2 filas de totales, árbitro y tarjetas: los
    tres gates viejos dicen "hecho" y el combate caía en el `continue`. Lo único
    que lo delata es que `has_round_stats` ahora significa COMPLETO. Y hay que
    reescribir también los TOTALES, no solo los asaltos que faltan: los guardados
    son los del R1 (46/69) haciéndose pasar por los del combate (155/229).
    """
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    client = _FakeClient({EVENT_URL: EVENT_HTML, FIGHT_URL: FIGHT_HTML})
    counts: Counter = Counter()
    consolidado = dict(has_stats=True, referee="Keith Peterson", has_scorecards=True)
    _fill_event(conn, client, None, 5, [_bout(has_round_stats=False, **consolidado)],
                EVENT_URL, counts, False)
    statements = [
        " ".join(sql.split()) for cur in conn.cursors for sql, _p in cur.executed
    ]
    assert any(s.startswith("INSERT INTO fight_stats ") for s in statements)
    assert any(s.startswith("INSERT INTO fight_stats_rounds") for s in statements)
    assert counts["stats_rows"] == 2 and counts["round_stats_rows"] == 4
    # El resultado y los oficiales ya estaban bien: no se tocan.
    assert not any(s.startswith("UPDATE fights") for s in statements)

    # Y el mismo combate CON su desglose completo se salta entero: ni un fetch
    # de la página del combate, que es lo que evita el churn diario.
    client = _FakeClient({EVENT_URL: EVENT_HTML, FIGHT_URL: FIGHT_HTML})
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    _fill_event(conn, client, None, 5, [_bout(has_round_stats=True, **consolidado)],
                EVENT_URL, Counter(), False)
    assert client.fetched == [EVENT_URL]
    assert fakedb.mutating_statements(conn) == []


# ------------------------------------------- backfill histórico (batch, dispatch)


def _backfill_responder(sql, params=None):
    flat = " ".join(sql.split())
    if "FROM fighters WHERE source" in flat:
        return {
            "/fighter-details/c814b4c899793af6": [(201,)],
            "/fighter-details/2e7878927067fdca": [(202,)],
        }.get(params[1], [])
    return []


def test_backfill_round_stats_processes_a_fight(fakedb):
    conn = fakedb.Connection(_backfill_responder)
    client = _FakeClient({FIGHT_URL: FIGHT_HTML})
    counts: Counter = Counter()
    process_round_backfill_fight(
        conn, client, counts, {}, 11, "/fight-details/c13dc0cccef263f7", dry_run=False
    )
    assert client.fetched == [FIGHT_URL]
    inserts = [
        params
        for cur in conn.cursors
        for sql, params in cur.executed
        if "INSERT INTO fight_stats_rounds" in sql
    ]
    assert [p[:3] for p in inserts] == [
        (11, 201, 1), (11, 202, 1), (11, 201, 2), (11, 202, 2),
    ]
    assert counts["round_rows"] == 4
    assert counts["fights_processed"] == 1


def test_backfill_round_stats_dry_run_never_writes(fakedb):
    conn = fakedb.Connection(_backfill_responder)
    client = _FakeClient({FIGHT_URL: FIGHT_HTML})
    counts: Counter = Counter()
    process_round_backfill_fight(
        conn, client, counts, {}, 11, "/fight-details/c13dc0cccef263f7", dry_run=True
    )
    assert fakedb.mutating_statements(conn) == []
    assert counts["round_rows"] == 4


def test_backfill_round_stats_counts_pages_without_rounds(fakedb):
    conn = fakedb.Connection(_backfill_responder)
    client = _FakeClient({FIGHT_URL: TOTALS_ONLY_HTML})
    counts: Counter = Counter()
    process_round_backfill_fight(
        conn, client, counts, {}, 11, FIGHT_URL, dry_run=False
    )
    assert counts["fights_without_rounds"] == 1
    assert fakedb.mutating_statements(conn) == []


def test_list_target_fights_query_shape(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    list_target_fights(conn, 365, 2000)
    sql, params = conn.cursors[0].executed[0]
    flat = " ".join(sql.split())
    assert "f.source = 'ufcstats'" in flat
    assert "EXISTS (SELECT 1 FROM fight_stats fs WHERE fs.fight_id = f.id)" in flat
    assert "NOT EXISTS (SELECT 1 FROM fight_stats_rounds fsr WHERE fsr.fight_id = f.id)" in flat
    assert flat.endswith("LIMIT %s")
    assert params == (365, 2000)
