"""The cleanup scripts are dry-run by default: no UPDATE/DELETE is executed and
nothing is committed unless --apply is given."""

import types

from src.scrapers import cleanup_data_quality as cdq
from src.scrapers import cleanup_non_espn as cne


def test_cleanup_non_espn_dry_run_writes_nothing(monkeypatch, fakedb):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if "information_schema" in flat:
            return [(True,)]
        if "COUNT(*)" in flat:
            return [(3,)]
        return []

    conn = fakedb.Connection(responder)
    monkeypatch.setattr(cne, "connect", lambda url: conn)
    monkeypatch.setattr(cne, "get_settings", lambda: types.SimpleNamespace(database_url="x"))

    summary = cne.cleanup_non_espn_fighters(apply=False)

    assert summary.dry_run is True
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0


def test_cleanup_data_quality_dry_run_writes_nothing(monkeypatch, fakedb):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if "GROUP BY" in flat:
            return []  # headshot-domain / duplicate-name aggregates: no rows
        if "SELECT COUNT(*)" in flat:
            return [(0,)]
        return []  # no suspicious rows, no headshots -> no work, no network

    conn = fakedb.Connection(responder)
    monkeypatch.setattr(cdq, "connect", lambda url: conn)
    monkeypatch.setattr(
        cdq,
        "get_settings",
        lambda: types.SimpleNamespace(database_url="x", user_agent="agent ufcstats.com"),
    )

    summary = cdq.cleanup_data_quality(apply=False)

    assert summary.dry_run is True
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
