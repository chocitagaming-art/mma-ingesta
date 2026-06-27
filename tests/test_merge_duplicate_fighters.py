"""merge_duplicate_fighters: dry-run by default + homonym guard."""

import types
from datetime import date

from src.scrapers import merge_duplicate_fighters as mod

# Column order of the SELECT in merge_duplicates().
# id, name, nickname, headshot_url, nationality, birth_date, height_cm, reach_cm,
# stance, weight_grams, wins, losses, draws, source, source_id


def _row(fighter_id, name, birth_date=None, nationality=None, headshot=None, source="ufcstats"):
    return (
        fighter_id, name, None, headshot, nationality, birth_date,
        None, None, None, None, 0, 0, 0, source, str(fighter_id),
    )


def _responder(rows):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if "FROM fighters" in flat and "ORDER BY lower(name)" in flat:
            return rows
        if "COUNT(*)" in flat:
            return [(0,)]
        return []

    return responder


def _patch(monkeypatch, fakedb, rows):
    conn = fakedb.Connection(_responder(rows))
    monkeypatch.setattr(mod, "connect", lambda url: conn)
    monkeypatch.setattr(mod, "get_settings", lambda: types.SimpleNamespace(database_url="x"))
    return conn


def test_skips_homonyms_without_force(monkeypatch, fakedb):
    rows = [
        _row(1, "John Smith", birth_date=date(1990, 1, 1)),
        _row(2, "John Smith", birth_date=date(1985, 5, 5)),
    ]
    conn = _patch(monkeypatch, fakedb, rows)
    result = mod.merge_duplicates(apply=True, force_homonyms=False)
    assert result["homonyms_skipped"] == 1
    assert result["groups_merged"] == 0
    assert not any("DELETE FROM FIGHTERS" in s.upper() for s in fakedb.mutating_statements(conn))


def test_force_homonyms_merges(monkeypatch, fakedb):
    rows = [
        _row(1, "John Smith", birth_date=date(1990, 1, 1)),
        _row(2, "John Smith", birth_date=date(1985, 5, 5)),
    ]
    conn = _patch(monkeypatch, fakedb, rows)
    result = mod.merge_duplicates(apply=True, force_homonyms=True)
    assert result["groups_merged"] == 1
    assert any("DELETE FROM fighters" in s for s in fakedb.mutating_statements(conn))


def test_dry_run_writes_nothing(monkeypatch, fakedb):
    rows = [_row(1, "Jane Doe"), _row(2, "Jane Doe")]  # genuine duplicate, no conflict
    conn = _patch(monkeypatch, fakedb, rows)
    result = mod.merge_duplicates(apply=False)
    assert result["groups_merged"] == 1  # previewed
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0
