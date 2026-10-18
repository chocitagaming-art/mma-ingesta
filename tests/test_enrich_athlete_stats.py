"""ufc.com career finish-stats (1A) parsing, guard, scope and write tests.

The HTML fixture mirrors the REAL hero block ufc.com serves (verified against
anna-melisano and cory-sandhagen on 2026-07-18): div.hero-profile__stats with
one div.hero-profile__stat per figure, each a .hero-profile__stat-numb (number)
and .hero-profile__stat-text (label). No network, no DB: the resolver is
injected and writes go through the shared fakedb recorder.
"""

from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from src.scrapers import enrich_athlete_stats
from src.scrapers.enrich_athlete_stats import (
    FinishStats,
    StatsPage,
    _identity_verified,
    parse_finish_stats,
)
from src.scrapers.enrich_fullbody import _unique_nickname_sql
from src.scrapers.repositories.fighters import (
    clear_fighter_social_links,
    count_fighter_social_links_set,
    fighter_social_columns_present,
    update_fighter_finish_stats,
    update_fighter_social_links,
)
from src.scrapers.ufc_social import SocialLinks

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
    assert _identity_verified("Anna Melisano", page, ufc_confirmed=False, nickname=None)
    other = StatsPage(stats=FinishStats(2, 1, 2), page_name="Somebody Else Entirely")
    assert not _identity_verified("Anna Melisano", other, ufc_confirmed=False, nickname=None)
    # No hero name: only trusted when the headshot already proved the page.
    anon = StatsPage(stats=FinishStats(2, 1, 2), page_name=None)
    assert _identity_verified("Anna Melisano", anon, ufc_confirmed=True, nickname=None)
    assert not _identity_verified("Anna Melisano", anon, ufc_confirmed=False, nickname=None)


def test_identity_guard_accepts_hero_name_equal_to_nickname():
    # Real case fighters.id=9132: ufc.com renders 'Tina Black' on Valesca
    # Machado's canonical page. Exact nickname -> verified; anything looser not.
    page = StatsPage(stats=FinishStats(1, 0, 1), page_name="Tina Black")
    assert _identity_verified("Valesca Machado", page, ufc_confirmed=False, nickname="Tina Black")
    assert _identity_verified("Valesca Machado", page, ufc_confirmed=False, nickname="Tína BLACK")
    assert not _identity_verified("Joe Smith", page, ufc_confirmed=False, nickname=None)
    assert not _identity_verified("Valesca Machado", page, ufc_confirmed=False, nickname="Black")
    assert not _identity_verified(
        "Valesca Machado", page, ufc_confirmed=False, nickname="Tina Black Jr"
    )


def test_identity_guard_without_page_name_ignores_nickname():
    anon = StatsPage(stats=FinishStats(1, 0, 1), page_name=None)
    assert _identity_verified("Valesca Machado", anon, ufc_confirmed=True, nickname="Tina Black")
    assert not _identity_verified(
        "Valesca Machado", anon, ufc_confirmed=False, nickname="Tina Black"
    )


# ------------------------------------------------------------------- backfill


_ANNA = (1, "Anna Melisano", None, False)  # (id, name, nickname, ufc_confirmed)


def _responder(update_result=None, target=_ANNA):
    def responder(sql, params=None):
        upper = sql.upper()
        if upper.strip().startswith("SELECT"):
            return [target]
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
    assert counts["nickname_match"] == 0  # accepted by NAME
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


def test_backfill_writes_stats_for_fighter_published_under_nickname(fakedb, caplog):
    target = (9132, "Valesca Machado", "Tina Black", False)
    conn = fakedb.Connection(_responder(update_result=[(1,)], target=target))
    with caplog.at_level("INFO", logger="src.scrapers.enrich_athlete_stats"):
        counts = enrich_athlete_stats.backfill(
            conn,
            resolver=lambda session, name: _page(page_name="Tina Black"),
            sleeper=lambda seconds: None,
        )
    assert counts["name_mismatch"] == 0
    assert counts["updated"] == 1
    assert len(fakedb.mutating_statements(conn)) == 1
    # The looser nickname path always leaves a trace: counter + INFO line.
    assert counts["nickname_match"] == 1
    traces = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Accepted by nickname")
    ]
    assert len(traces) == 1 and "id=9132" in traces[0] and "'Tina Black'" in traces[0]


def test_backfill_unresolved_page_counts(fakedb):
    conn = fakedb.Connection(_responder())
    counts = enrich_athlete_stats.backfill(
        conn,
        resolver=lambda session, name: None,
        sleeper=lambda seconds: None,
    )
    assert counts["unresolved"] == 1
    assert fakedb.mutating_statements(conn) == []


# ------------------------------------------- Neon idle-in-transaction timeout

# (targets, positions whose stats are ALREADY stored — the IS DISTINCT FROM
# guard makes their UPDATE hit 0 rows —, positions with NEW stats, seconds the
# FIRST fetch takes). Every other fighter has no stats block, so it runs no SQL
# at all. The fake sleeper advances the clock by that first gap on fighter 1
# and 60 s on every later one: the gaps between statements are minutes.
_NEON_SCENARIOS = {
    # Fighter 1's no-op UPDATE opens a transaction, 2-7 run no SQL, and
    # fighter 8's UPDATE arrives 7 min later: dead unless EVERY iteration
    # closes its transaction, not only the ones that changed a row.
    "noop-update-then-7-min-gap": (8, {1}, {8}, 60),
    # ufc.com takes 6 min on fighter 1, so its UPDATE (the first statement after
    # the target SELECT) arrives 6 min after it, before any end-of-iteration
    # commit: dead if the read transaction of the SELECT is still open, in
    # write mode too. In dry-run it would stay open to the end.
    "first-sql-6-min-after-select": (8, {1}, {8}, 360),
}


def _neon_targets(total):
    # Ids from 101 so they never collide with the stat values in UPDATE params.
    return [(100 + i, f"Fighter Number{i}", None, False) for i in range(1, total + 1)]


def _neon_responder(targets, stored_ids):
    def responder(sql, params=None):
        upper = sql.upper()
        if upper.strip().startswith("SELECT"):
            return targets
        if "UPDATE" in upper:
            return [] if stored_ids.intersection(params) else [(1,)]
        return []

    return responder


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
@pytest.mark.parametrize("scenario", sorted(_NEON_SCENARIOS))
def test_backfill_survives_neon_idle_in_transaction_timeout(fakedb, scenario, dry_run):
    # Raising the workflow timeout alone would walk the monthly sweep into this:
    # enrich-facts (same loop) died of this exact OperationalError on
    # 1-sep-2026 (run 33484806063).
    total, stored, new, first_gap = _NEON_SCENARIOS[scenario]
    targets = _neon_targets(total)
    with_stats = {targets[i - 1][1] for i in stored | new}
    gaps = iter([first_gap] + [60] * (total - 1))
    clock = fakedb.NeonClock()
    conn = fakedb.NeonLikeConnection(
        _neon_responder(targets, {targets[i - 1][0] for i in stored}), clock
    )
    counts = enrich_athlete_stats.backfill(
        conn,
        dry_run=dry_run,
        all_scope=True,
        resolver=lambda session, name: _page(
            stats=FinishStats(2, 1, 2) if name in with_stats else None, page_name=name
        ),
        sleeper=lambda seconds: clock.advance(next(gaps)),
    )
    assert counts["with_stats"] == 2
    assert clock.now == first_gap + 60 * (total - 1)  # every fighter was visited
    assert not conn.in_txn  # nothing left open behind the sweep
    if dry_run:
        assert counts["would_update"] == 2
        assert fakedb.mutating_statements(conn) == []
        assert conn.commits == 0
    else:
        assert counts["updated"] == 1  # the already-stored fighter is a no-op
        assert len(fakedb.mutating_statements(conn)) == 2


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
def test_backfill_holds_no_transaction_while_fetching_ufc_com(fakedb, dry_run):
    """The invariant behind the scenarios above, independent of timing: when a
    page is fetched, neither the target SELECT nor any earlier UPDATE (0-row
    ones included) may still hold a transaction. A sweep spends ~30 min on
    ufc.com, and a single slow first fetch is enough to cross Neon's 5 min."""
    targets = _neon_targets(4)
    conn = fakedb.NeonLikeConnection(
        _neon_responder(targets, {targets[0][0]}), fakedb.NeonClock()
    )
    open_while_fetching = []

    def resolver(session, name):
        if conn.in_txn:
            open_while_fetching.append(name)
        return _page(stats=FinishStats(2, 1, 2), page_name=name)

    counts = enrich_athlete_stats.backfill(
        conn, dry_run=dry_run, all_scope=True, resolver=resolver,
        sleeper=lambda seconds: None,
    )
    assert counts["with_stats"] == 4
    assert open_while_fetching == []
    assert not conn.in_txn


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


def test_target_selection_brings_the_nickname_in_both_scopes(fakedb):
    # Rows are unpacked by POSITION as (id, name, nickname, ufc_confirmed): pin
    # the exact SELECT list so a reordered column fails here, not in production.
    nickname = _unique_nickname_sql("f.nickname")
    columns = f"f.id, f.name, {nickname}, (f.headshot_url ILIKE %s) AS ufc_confirmed"
    expected = {
        True: f"SELECT {columns} FROM fighters f WHERE ",
        False: f"SELECT DISTINCT {columns} FROM fighters f JOIN fights fi ",
    }
    rows = [(9132, "Valesca Machado", "Tina Black", False), (1, "Anna Melisano", "", True)]
    for all_scope in (True, False):
        conn = fakedb.Connection(lambda sql, params=None: rows)
        targets = enrich_athlete_stats._get_target_fighters(conn, all_scope=all_scope)
        flat = " ".join(conn.cursors[0].executed[0][0].split())
        assert flat.startswith(expected[all_scope])
        assert targets == [
            (9132, "Valesca Machado", "Tina Black", False),
            (1, "Anna Melisano", None, True),
        ]


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


# ------------------------------------------------- social links (migration 032)

_VOLK = (7029, "Alexander Volkanovski", None, False)
_VOLK_LINKS = SocialLinks(
    instagram_url="https://www.instagram.com/alexvolkanovski/",
    x_url="https://x.com/alexvolkanovski",
)


def _social_responder(
    *, columns=True, update_result=None, target=_VOLK, targets=None, stored_count=0
):
    """Like _responder, plus the information_schema check of migration 032 and
    the dry-run's read-only count of links that would be deleted."""

    def responder(sql, params=None):
        upper = sql.upper()
        if "INFORMATION_SCHEMA" in upper:
            return [(2,)] if columns else [(0,)]
        if "COUNT(*) FROM FIGHTERS" in upper:
            return [(stored_count,)]
        if upper.strip().startswith("SELECT"):
            return targets if targets is not None else [target]
        if "UPDATE" in upper:
            return update_result or []
        return []

    return responder


def _social_params(conn):
    """Params of every inline social-links UPDATE (sets, never clears)."""
    return [
        params
        for cur in conn.cursors
        for sql, params in cur.executed
        if "COALESCE(%s, instagram_url)" in sql
    ]


def _clears(conn):
    """(column, fighter ids) of every end-of-run deletion statement."""
    out = []
    for cur in conn.cursors:
        for sql, params in cur.executed:
            flat = " ".join(sql.split())
            for column in ("instagram_url", "x_url"):
                if flat.startswith(f"UPDATE fighters SET {column} = NULL"):
                    out.append((column, params[0]))
    return out


def _run(conn, page, **kwargs):
    return enrich_athlete_stats.backfill(
        conn, resolver=lambda session, name: page, sleeper=lambda seconds: None, **kwargs
    )


def test_backfill_writes_social_links_with_the_stats(fakedb):
    conn = fakedb.Connection(_social_responder(update_result=[(1,)]))
    page = StatsPage(
        stats=FinishStats(13, 3, 6), page_name="Alexander Volkanovski", socials=_VOLK_LINKS
    )
    counts = _run(conn, page)
    assert counts["updated"] == 1
    assert counts["with_socials"] == 1
    assert counts["with_instagram"] == 1 and counts["with_x"] == 1
    assert counts["socials_updated"] == 1
    assert counts["socials_columns_missing"] == 0
    assert _social_params(conn) == [(
        "https://www.instagram.com/alexvolkanovski/", "https://x.com/alexvolkanovski", 7029,
        "https://www.instagram.com/alexvolkanovski/", "https://x.com/alexvolkanovski",
    )]
    assert conn.commits == 1  # both UPDATEs closed by the per-fighter commit


def test_backfill_block_without_a_network_deletes_it_at_the_end(fakedb):
    # Owner's rule: ufc.com stopped listing the Instagram (block present) ->
    # the stored link is DELETED, not kept. The X is set inline; the deletion
    # waits for the end of the run (the brake), then goes out and commits.
    conn = fakedb.Connection(_social_responder(update_result=[(1,)]))
    page = StatsPage(
        stats=None,
        page_name="Alexander Volkanovski",
        socials=SocialLinks(instagram_url=None, x_url="https://x.com/alexvolkanovski"),
    )
    counts = _run(conn, page)
    assert counts["socials_block"] == 1
    assert counts["with_socials"] == 1 and counts["with_instagram"] == 0
    (params,) = _social_params(conn)
    assert params[0] is None and params[1] == "https://x.com/alexvolkanovski"
    assert _clears(conn) == [("instagram_url", [7029])]
    assert counts["socials_cleared"] == 1
    assert counts["socials_brake"] == 0
    assert conn.commits == 2  # the per-fighter commit + the end-of-run deletion


def test_backfill_empty_block_clears_both(fakedb):
    conn = fakedb.Connection(_social_responder(update_result=[(1,)]))
    page = StatsPage(
        stats=None, page_name="Alexander Volkanovski", socials=SocialLinks(None, None)
    )
    counts = _run(conn, page)
    assert counts["with_socials"] == 0
    assert counts["socials_updated"] == 0
    assert _social_params(conn) == []  # nothing to set: no inline round trip
    assert _clears(conn) == [("instagram_url", [7029]), ("x_url", [7029])]
    assert counts["socials_cleared"] == 2


def test_backfill_unreadable_network_is_kept_not_deleted(fakedb, caplog):
    # ufc.com lists an X in a shape we cannot read (say x.com/i/user/123):
    # that is not "dropped", so the stored X stays.
    conn = fakedb.Connection(_social_responder(update_result=[(1,)]))
    page = StatsPage(
        stats=None,
        page_name="Alexander Volkanovski",
        socials=SocialLinks(
            "https://www.instagram.com/alexvolkanovski/", None, x_unparsed=True
        ),
    )
    with caplog.at_level("WARNING", logger="src.scrapers.enrich_athlete_stats"):
        counts = _run(conn, page)
    assert counts["socials_unparsed"] == 1
    assert _clears(conn) == []
    assert _social_params(conn)[0][:2] == ("https://www.instagram.com/alexvolkanovski/", None)
    assert any("kept as stored" in r.getMessage() for r in caplog.records)


def _brake_run(fakedb, pages, **kwargs):
    """Run the backfill over one target per page (page i for target i)."""
    targets = _neon_targets(len(pages))
    by_name = {name: page for (_, name, _, _), page in zip(targets, pages)}
    conn = fakedb.Connection(
        _social_responder(update_result=[(1,)], targets=targets, **kwargs.pop("responder", {}))
    )
    counts = enrich_athlete_stats.backfill(
        conn,
        all_scope=True,
        resolver=lambda session, name: StatsPage(None, name, by_name[name]),
        sleeper=lambda seconds: None,
        **kwargs,
    )
    return conn, counts, targets


_BOTH = SocialLinks("https://www.instagram.com/someone/", "https://x.com/someone")
_NONE = SocialLinks(None, None)


def test_brake_holds_every_deletion_when_ufc_com_stops_listing_them(fakedb, caplog):
    # 🪤 The scenario of the review: ufc.com renames/drops sameAs in its
    # template, so EVERY Person block "lists nothing". Without the brake the
    # sweep would NULL every fighter's links, in green.
    pages = [_NONE] * enrich_athlete_stats.SOCIALS_BRAKE_MIN_PAGES
    with caplog.at_level("ERROR", logger="src.scrapers.enrich_athlete_stats"):
        conn, counts, _ = _brake_run(fakedb, pages)
    assert _clears(conn) == []
    assert counts["socials_brake"] == 1
    assert counts["socials_clears_held"] == 2 * len(pages)
    assert counts["socials_cleared"] == 0
    assert any("NOT deleting" in r.getMessage() for r in caplog.records)


def test_brake_is_per_network(fakedb):
    # A new X URL shape nobody parses: X looks dropped everywhere, Instagram
    # is fine. Only the X deletions are held.
    n = enrich_athlete_stats.SOCIALS_BRAKE_MIN_PAGES
    ig_only = SocialLinks("https://www.instagram.com/someone/", None)
    pages = [ig_only] * (n - 3) + [_NONE] * 3
    conn, counts, targets = _brake_run(fakedb, pages)
    assert _clears(conn) == [("instagram_url", [t[0] for t in targets[-3:]])]
    assert counts["socials_brake"] == 1
    assert counts["socials_clears_held"] == n  # every pending X deletion


def test_healthy_run_deletes_what_ufc_com_dropped(fakedb):
    # Shares like the measured ones (most list both): deletions go through.
    n = enrich_athlete_stats.SOCIALS_BRAKE_MIN_PAGES
    pages = [_BOTH] * (n - 5) + [_NONE] * 5
    conn, counts, targets = _brake_run(fakedb, pages)
    dropped = [t[0] for t in targets[-5:]]
    assert _clears(conn) == [("instagram_url", dropped), ("x_url", dropped)]
    assert counts["socials_brake"] == 0
    assert counts["socials_clears_held"] == 0


def test_small_run_below_the_minimum_is_not_judged(fakedb):
    # The daily upcoming-cards run can be small: below MIN_PAGES the shares
    # mean nothing, so deletions go through as the owner decided.
    pages = [_NONE] * (enrich_athlete_stats.SOCIALS_BRAKE_MIN_PAGES - 1)
    conn, counts, _ = _brake_run(fakedb, pages)
    assert counts["socials_brake"] == 0
    assert len(_clears(conn)) == 2


def test_brake_fires_when_person_blocks_disappear(fakedb):
    # ufc.com drops the JSON-LD block: nothing gets deleted (None = unknown),
    # but the fill has stopped silently, so the run still raises the alarm.
    pages = [None] * enrich_athlete_stats.SOCIALS_BRAKE_MIN_PAGES
    conn, counts, _ = _brake_run(fakedb, pages)
    assert counts["no_socials_block"] == len(pages)
    assert counts["socials_brake"] == 1
    assert fakedb.mutating_statements(conn) == []


def test_dry_run_reports_what_it_would_delete_read_only(fakedb):
    pages = [_BOTH, _NONE]
    conn, counts, _ = _brake_run(fakedb, pages, dry_run=True, responder={"stored_count": 1})
    assert counts["socials_would_clear"] == 2  # one stored link per network
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_dry_run_brake_also_fires(fakedb):
    pages = [_NONE] * enrich_athlete_stats.SOCIALS_BRAKE_MIN_PAGES
    conn, counts, _ = _brake_run(fakedb, pages, dry_run=True)
    assert counts["socials_brake"] == 1
    assert counts["socials_would_clear"] == 0
    assert fakedb.mutating_statements(conn) == []


def test_main_exits_1_when_the_brake_fires(monkeypatch, capsys):
    from collections import Counter
    from contextlib import nullcontext

    monkeypatch.setattr(enrich_athlete_stats, "configure_logging", lambda: None)
    monkeypatch.setattr(
        enrich_athlete_stats, "get_settings", lambda: SimpleNamespace(database_url="x")
    )
    monkeypatch.setattr(enrich_athlete_stats, "connect", lambda url: nullcontext(object()))
    monkeypatch.setattr(
        enrich_athlete_stats, "backfill", lambda conn, **kw: Counter(socials_brake=1)
    )
    monkeypatch.setattr("sys.argv", ["enrich_athlete_stats", "--all"])
    with pytest.raises(SystemExit) as exc:
        enrich_athlete_stats.main()
    assert exc.value.code == 1
    assert '"socials_brake": 1' in capsys.readouterr().out


def test_main_exits_0_without_the_brake(monkeypatch):
    from collections import Counter
    from contextlib import nullcontext

    monkeypatch.setattr(enrich_athlete_stats, "configure_logging", lambda: None)
    monkeypatch.setattr(
        enrich_athlete_stats, "get_settings", lambda: SimpleNamespace(database_url="x")
    )
    monkeypatch.setattr(enrich_athlete_stats, "connect", lambda url: nullcontext(object()))
    monkeypatch.setattr(enrich_athlete_stats, "backfill", lambda conn, **kw: Counter(targets=3))
    monkeypatch.setattr("sys.argv", ["enrich_athlete_stats"])
    enrich_athlete_stats.main()  # no SystemExit


def test_backfill_without_jsonld_block_touches_nothing(fakedb):
    # No Person block = "we do not know": never NULL the stored links.
    conn = fakedb.Connection(_social_responder(update_result=[(1,)]))
    page = StatsPage(
        stats=FinishStats(13, 3, 6), page_name="Alexander Volkanovski", socials=None
    )
    counts = _run(conn, page)
    assert counts["no_socials_block"] == 1
    assert counts["socials_block"] == 0
    assert _social_params(conn) == []
    assert counts["updated"] == 1  # the stats still go through


def test_backfill_writes_socials_for_pages_without_stats_block(fakedb):
    # Retired fighters (Gerald Harris, Julie Kedzie) have no stats block but do
    # list an X account.
    conn = fakedb.Connection(_social_responder(update_result=[(1,)]))
    page = StatsPage(stats=None, page_name="Alexander Volkanovski", socials=_VOLK_LINKS)
    counts = _run(conn, page)
    assert counts["no_stats"] == 1
    assert counts["socials_updated"] == 1
    assert len(fakedb.mutating_statements(conn)) == 1


def test_backfill_name_mismatch_never_writes_socials(fakedb):
    conn = fakedb.Connection(_social_responder(update_result=[(1,)]))
    page = StatsPage(
        stats=FinishStats(1, 1, 1), page_name="Somebody Else Entirely", socials=_VOLK_LINKS
    )
    counts = _run(conn, page)
    assert counts["name_mismatch"] == 1
    assert counts["with_socials"] == 0
    assert fakedb.mutating_statements(conn) == []


def test_backfill_dry_run_counts_socials_writes_nothing(fakedb):
    conn = fakedb.Connection(_social_responder(update_result=[(1,)]))
    page = StatsPage(
        stats=FinishStats(13, 3, 6), page_name="Alexander Volkanovski", socials=_VOLK_LINKS
    )
    counts = _run(conn, page, dry_run=True)
    assert counts["with_socials"] == 1
    assert counts["socials_updated"] == 0
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_backfill_before_migration_032_keeps_refreshing_stats(fakedb, caplog):
    # Code deployed before the migration: stats are written as always, socials
    # are only counted, and the run says so (summary + one WARNING).
    conn = fakedb.Connection(_social_responder(columns=False, update_result=[(1,)]))
    page = StatsPage(
        stats=FinishStats(13, 3, 6), page_name="Alexander Volkanovski", socials=_VOLK_LINKS
    )
    with caplog.at_level("WARNING", logger="src.scrapers.enrich_athlete_stats"):
        counts = _run(conn, page)
    assert counts["socials_columns_missing"] == 1
    assert counts["updated"] == 1
    assert counts["with_socials"] == 1 and counts["socials_updated"] == 0
    assert _social_params(conn) == []
    assert any("migration 032" in r.getMessage() for r in caplog.records)


def test_backfill_column_check_rides_the_released_read_transaction(fakedb):
    # The information_schema check must happen inside the target SELECT's
    # transaction, which is released BEFORE ufc.com is fetched (Neon kills a
    # session idle in transaction for 5 min).
    targets = _neon_targets(2)
    conn = fakedb.NeonLikeConnection(
        _social_responder(target=targets[0]), fakedb.NeonClock()
    )
    open_while_fetching = []

    def resolver(session, name):
        if conn.in_txn:
            open_while_fetching.append(name)
        return StatsPage(stats=None, page_name=name, socials=_VOLK_LINKS)

    enrich_athlete_stats.backfill(
        conn, all_scope=True, resolver=resolver, sleeper=lambda seconds: None
    )
    assert open_while_fetching == []
    assert not conn.in_txn


class _FakeResponse:
    def __init__(self, text, url, ok=True):
        self.text, self.url, self.ok = text, url, ok


def _session_returning(response):
    return SimpleNamespace(get=lambda url, headers=None, timeout=None: response)


def test_resolve_stats_parses_socials_from_the_same_page():
    html = (
        '<html><head><script type="application/ld+json">{"@graph": [{"@type": "WebPage",'
        '"mainEntity": {"@type": "Person", "url": "https://www.ufc.com/athlete/gerald-harris",'
        '"sameAs": ["https://twitter.com/ghurricane"]}}]}</script></head><body>'
        '<h1 class="hero-profile__name">Gerald Harris</h1>'
        '<a href="https://www.instagram.com/ufc/" class="c-menu-footer__link--level-2">'
        "Instagram</a></body></html>"
    )
    response = _FakeResponse(html, "https://www.ufc.com/athlete/gerald-harris")
    page = enrich_athlete_stats.resolve_stats(_session_returning(response), "Gerald Harris")
    assert page == StatsPage(
        stats=None,
        page_name="Gerald Harris",
        socials=SocialLinks(instagram_url=None, x_url="https://x.com/ghurricane"),
    )


def test_resolve_stats_treats_search_redirect_as_unresolved():
    # An unknown slug answers 200 after redirecting to /search.
    response = _FakeResponse(
        "<html><body><h1>Search</h1></body></html>",
        "https://www.ufc.com/search?query=hector-santiago",
    )
    page = enrich_athlete_stats.resolve_stats(_session_returning(response), "Hector Santiago")
    assert page is None


def test_update_social_links_sets_but_never_clears(fakedb):
    # Inline writes only SET: a None keeps the stored value. Deleting is the
    # end-of-run step behind the brake (clear_fighter_social_links).
    conn = fakedb.Connection(lambda sql, params=None: [])
    update_fighter_social_links(conn, 7, instagram_url=None, x_url="https://x.com/a")
    (sql,) = fakedb.mutating_statements(conn)
    assert (
        "SET instagram_url = COALESCE(%s, instagram_url), x_url = COALESCE(%s, x_url), "
        "updated_at = NOW()"
    ) in sql
    assert (
        "instagram_url IS DISTINCT FROM COALESCE(%s, instagram_url) "
        "OR x_url IS DISTINCT FROM COALESCE(%s, x_url)"
    ) in sql
    params = conn.cursors[0].executed[0][1]
    assert params == (None, "https://x.com/a", 7, None, "https://x.com/a")


def test_clear_social_links_one_statement_only_rows_with_a_link(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(1,), (1,)])
    assert clear_fighter_social_links(conn, "x_url", [3, 5, 8]) == 2
    (sql,) = fakedb.mutating_statements(conn)
    assert sql == (
        "UPDATE fighters SET x_url = NULL, updated_at = NOW() "
        "WHERE id = ANY(%s) AND x_url IS NOT NULL"
    )
    assert conn.cursors[0].executed[0][1] == ([3, 5, 8],)


def test_clear_and_count_social_links_reject_other_columns_and_skip_empty(fakedb):
    conn = fakedb.Connection(lambda sql, params=None: [(4,)])
    with pytest.raises(ValueError):
        clear_fighter_social_links(conn, "name", [1])
    with pytest.raises(ValueError):
        count_fighter_social_links_set(conn, "headshot_url; DROP TABLE fighters", [1])
    assert clear_fighter_social_links(conn, "x_url", []) == 0
    assert count_fighter_social_links_set(conn, "x_url", []) == 0
    assert conn.cursors == []  # no SQL at all for an empty list
    assert count_fighter_social_links_set(conn, "instagram_url", [1, 2]) == 4
    sql, params = conn.cursors[0].executed[0]
    assert "SELECT count(*) FROM fighters" in sql and "UPDATE" not in sql.upper()
    assert params == ([1, 2],)


@pytest.mark.parametrize("found,expected", [(2, True), (1, False), (0, False)])
def test_social_columns_present_reads_information_schema(fakedb, found, expected):
    conn = fakedb.Connection(lambda sql, params=None: [(found,)])
    assert fighter_social_columns_present(conn) is expected
    sql, params = conn.cursors[0].executed[0]
    assert "information_schema.columns" in sql
    assert params == (("instagram_url", "x_url"),)
