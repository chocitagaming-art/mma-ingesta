"""ufc.com career finish-stats (1A) parsing, guard, scope and write tests.

The HTML fixture mirrors the REAL hero block ufc.com serves (verified against
anna-melisano and cory-sandhagen on 2026-07-18): div.hero-profile__stats with
one div.hero-profile__stat per figure, each a .hero-profile__stat-numb (number)
and .hero-profile__stat-text (label). No network, no DB: the resolver is
injected and writes go through the shared fakedb recorder.
"""

from bs4 import BeautifulSoup

from src.scrapers import enrich_athlete_stats
from src.scrapers.enrich_athlete_stats import (
    FinishStats,
    StatsPage,
    _identity_verified,
    parse_finish_stats,
)
from src.scrapers.repositories.fighters import update_fighter_finish_stats

# ------------------------------------------------------------------- fixtures


def _stats_block(ko="2", sub="1", first="2", *, order=("ko", "sub", "first")):
    cells = {
        "ko": f'<div class="hero-profile__stat"><p class="hero-profile__stat-numb">{ko}</p>'
        '<p class="hero-profile__stat-text">Wins by Knockout</p></div>',
        "sub": f'<div class="hero-profile__stat"><p class="hero-profile__stat-numb">{sub}</p>'
        '<p class="hero-profile__stat-text">Wins by Submission</p></div>',
        "first": f'<div class="hero-profile__stat"><p class="hero-profile__stat-numb">{first}</p>'
        '<p class="hero-profile__stat-text">First Round Finishes</p></div>',
    }
    inner = "".join(cells[k] for k in order if k in cells)
    return f'<div class="hero-profile__stats">{inner}</div>'


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# --------------------------------------------------------------------- parsing


def test_parse_stats_reads_three_by_label():
    stats = parse_finish_stats(_soup(_stats_block("2", "1", "2")))
    assert stats == FinishStats(wins_by_ko=2, wins_by_submission=1, first_round_finishes=2)


def test_parse_stats_is_order_independent():
    # Column order is not trusted; mapping is by label text.
    html = _stats_block("8", "3", "6", order=("first", "ko", "sub"))
    stats = parse_finish_stats(_soup(html))
    assert stats == FinishStats(wins_by_ko=8, wins_by_submission=3, first_round_finishes=6)


def test_parse_stats_absent_container_returns_none():
    assert parse_finish_stats(_soup("<div><p>no stats here</p></div>")) is None


def test_parse_stats_unrecognized_labels_return_none():
    html = (
        '<div class="hero-profile__stats"><div class="hero-profile__stat">'
        '<p class="hero-profile__stat-numb">5</p>'
        '<p class="hero-profile__stat-text">Fight Win Streak</p></div></div>'
    )
    assert parse_finish_stats(_soup(html)) is None


def test_parse_stats_zero_is_a_valid_reading():
    # A pure-decision fighter really has 0 finishes: the block is present, so
    # 0/0/0 must parse (NOT None) and later be stored, matching ufc.com.
    stats = parse_finish_stats(_soup(_stats_block("0", "0", "0")))
    assert stats == FinishStats(0, 0, 0)


def test_parse_stats_missing_one_stat_defaults_to_zero():
    # Container present but ufc.com omitted the submission figure -> 0.
    html = _stats_block("4", order=("ko", "first"))
    stats = parse_finish_stats(_soup(html))
    assert stats == FinishStats(wins_by_ko=4, wins_by_submission=0, first_round_finishes=2)


# ----------------------------------------------------------------------- guard


def test_identity_guard_matches_and_rejects():
    page = StatsPage(stats=FinishStats(2, 1, 2), page_name="Anna Melisano")
    assert _identity_verified("Anna Melisano", page, ufc_confirmed=False)
    other = StatsPage(stats=FinishStats(2, 1, 2), page_name="Somebody Else Entirely")
    assert not _identity_verified("Anna Melisano", other, ufc_confirmed=False)
    # No hero name: only trusted when the headshot already proved the page.
    anon = StatsPage(stats=FinishStats(2, 1, 2), page_name=None)
    assert _identity_verified("Anna Melisano", anon, ufc_confirmed=True)
    assert not _identity_verified("Anna Melisano", anon, ufc_confirmed=False)


# ------------------------------------------------------------------- backfill


def _responder(update_result=None):
    def responder(sql, params=None):
        upper = sql.upper()
        if upper.strip().startswith("SELECT"):
            return [(1, "Anna Melisano", False)]  # one target: (id, name, ufc_confirmed)
        if "UPDATE" in upper:
            return update_result or []
        return []

    return responder


def _page(stats=FinishStats(2, 1, 2), page_name="Anna Melisano"):
    return StatsPage(stats=stats, page_name=page_name)


def test_backfill_writes_stats_and_commits(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_athlete_stats.backfill(
        conn,
        resolver=lambda session, name: _page(),
        sleeper=lambda seconds: None,
    )
    assert counts["updated"] == 1
    updates = fakedb.mutating_statements(conn)
    assert len(updates) == 1
    assert "wins_by_ko = %s" in updates[0]
    assert "wins_by_submission = %s" in updates[0]
    assert "first_round_finishes = %s" in updates[0]
    assert "IS DISTINCT FROM" in updates[0]
    assert conn.commits == 1


def test_backfill_dry_run_writes_nothing(fakedb):
    conn = fakedb.Connection(_responder(update_result=[(1,)]))
    counts = enrich_athlete_stats.backfill(
        conn,
        dry_run=True,
        resolver=lambda session, name: _page(),
        sleeper=lambda seconds: None,
    )
    assert counts["would_update"] == 1
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_backfill_skips_pages_without_stats_block(fakedb):
    conn = fakedb.Connection(_responder())
    counts = enrich_athlete_stats.backfill(
        conn,
        resolver=lambda session, name: _page(stats=None),
        sleeper=lambda seconds: None,
    )
    assert counts["no_stats"] == 1
    assert fakedb.mutating_statements(conn) == []


def test_backfill_name_mismatch_never_writes(fakedb):
    conn = fakedb.Connection(_responder())
    counts = enrich_athlete_stats.backfill(
        conn,
        resolver=lambda session, name: _page(page_name="Somebody Else Entirely"),
        sleeper=lambda seconds: None,
    )
    assert counts["name_mismatch"] == 1
    assert fakedb.mutating_statements(conn) == []


def test_backfill_unresolved_page_counts(fakedb):
    conn = fakedb.Connection(_responder())
    counts = enrich_athlete_stats.backfill(
        conn,
        resolver=lambda session, name: None,
        sleeper=lambda seconds: None,
    )
    assert counts["unresolved"] == 1
    assert fakedb.mutating_statements(conn) == []


def test_target_selection_scopes_and_homonym_safe(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    enrich_athlete_stats._get_target_fighters(conn, all_scope=True)
    enrich_athlete_stats._get_target_fighters(conn, all_scope=False)
    assert len(conn.cursors) == 2
    for cur in conn.cursors:
        sql = " ".join(cur.executed[0][0].split())
        assert "lower(dup.name) = lower(f.name)" in sql
        # New-value-wins: the scope must NOT filter out already-populated rows.
        assert "wins_by_ko IS NULL" not in sql
    upcoming_sql = " ".join(conn.cursors[1].executed[0][0].split())
    assert "e.status = 'upcoming'" in upcoming_sql


def test_target_selection_supports_limit_and_offset(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    enrich_athlete_stats._get_target_fighters(conn, all_scope=True, limit=1000, offset=2000)
    sql = " ".join(conn.cursors[0].executed[0][0].split())
    params = conn.cursors[0].executed[0][1]
    assert "LIMIT %s" in sql and "OFFSET %s" in sql
    assert params == ("%ufc.com%", 1000, 2000)


# ------------------------------------------------------------------ repository


def test_update_finish_stats_sql_is_new_value_wins(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    update_fighter_finish_stats(
        conn, 7, wins_by_ko=2, wins_by_submission=1, first_round_finishes=2
    )
    sql = " ".join(fakedb.mutating_statements(conn)[0].split())
    assert "wins_by_ko = %s" in sql
    assert "wins_by_submission = %s" in sql
    assert "first_round_finishes = %s" in sql
    # New-value-wins with a churn guard, NOT the additive COALESCE of facts.
    assert "COALESCE" not in sql
    assert "wins_by_ko IS DISTINCT FROM %s" in sql


def test_update_finish_stats_none_component_is_noop(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [])
    assert (
        update_fighter_finish_stats(
            conn, 7, wins_by_ko=None, wins_by_submission=1, first_round_finishes=2
        )
        is False
    )
    assert fakedb.mutating_statements(conn) == []


def test_update_finish_stats_zero_triple_is_written(fakedb):
    # 0/0/0 is a real reading (pure-decision fighter): it must reach SQL so the
    # stored value stops falling back to the UFC-only computation.
    conn = fakedb.Connection(lambda sql, params=None: [(1,)])
    update_fighter_finish_stats(
        conn, 7, wins_by_ko=0, wins_by_submission=0, first_round_finishes=0
    )
    assert len(fakedb.mutating_statements(conn)) == 1
