"""consolidate_fighters: homonym guard on merge, shared-fight guard on delete,
and dry-run by default (no writes)."""

from collections import Counter
from datetime import date

from src.scrapers import consolidate_fighters as cons


# merge_duplicates() join columns:
# espn_id, ufc_id, name, e.headshot, e.nat, e.birth, e.height, e.reach, e.weight,
# e.nick, u.headshot, u.nat, u.birth, u.height, u.reach, u.weight, u.nick
def _join_row(espn_id, ufc_id, name, e_birth=None, u_birth=None, e_nat=None, u_nat=None):
    return (
        espn_id, ufc_id, name,
        None, e_nat, e_birth, None, None, None, None,
        None, u_nat, u_birth, None, None, None, None,
    )


def _merge_responder(rows):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if "JOIN fighters u" in flat:
            return rows
        if "information_schema" in flat:
            return [(False,)]  # rankings table absent -> skip its reassignment
        return []

    return responder


def test_merge_skips_homonyms_without_force(fakedb):
    rows = [_join_row(101, 201, "John Smith", e_birth=date(1990, 1, 1), u_birth=date(1985, 5, 5))]
    conn = fakedb.Connection(_merge_responder(rows))
    counts: Counter = Counter()
    merges = cons.merge_duplicates(conn, counts, apply=True, force_homonyms=False)
    assert counts["homonyms_skipped"] == 1
    assert merges == []
    assert not any("DELETE FROM fighters" in s for s in fakedb.mutating_statements(conn))


def test_merge_force_homonyms(fakedb):
    rows = [_join_row(101, 201, "John Smith", e_birth=date(1990, 1, 1), u_birth=date(1985, 5, 5))]
    conn = fakedb.Connection(_merge_responder(rows))
    counts: Counter = Counter()
    cons.merge_duplicates(conn, counts, apply=True, force_homonyms=True)
    assert counts["duplicates_merged"] == 1
    assert any("DELETE FROM fighters" in s for s in fakedb.mutating_statements(conn))


def test_merge_dry_run_writes_nothing(fakedb):
    rows = [_join_row(101, 201, "Jane Doe")]  # no identity conflict
    conn = fakedb.Connection(_merge_responder(rows))
    counts: Counter = Counter()
    merges = cons.merge_duplicates(conn, counts, apply=False)
    assert counts["duplicates_merged"] == 1  # previewed
    assert fakedb.mutating_statements(conn) == []
    assert conn.commits == 0 and conn.rollbacks >= 1


def test_delete_sherdog_protects_shared_fight(fakedb):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if "WHERE source = %s" in flat:
            return [(5,)]
        if "SELECT EXISTS" in flat and "FROM fights" in flat:
            return [(True,)]
        return []

    conn = fakedb.Connection(responder)
    counts: Counter = Counter()
    deleted = cons.delete_sherdog_fighters(conn, counts, apply=True)
    assert deleted == []
    assert counts["sherdog_protected_shared"] == 1
    assert not any("DELETE FROM fighters" in s for s in fakedb.mutating_statements(conn))


def test_delete_sherdog_deletes_unshared_fighter(fakedb):
    def responder(sql, params=None):
        flat = " ".join(sql.split())
        if "WHERE source = %s" in flat:
            return [(7,)]
        if "SELECT EXISTS" in flat and "FROM fights" in flat:
            return [(False,)]  # no shared fight
        if "information_schema" in flat:
            return [(True,)]  # rankings table exists
        return []

    conn = fakedb.Connection(responder)
    counts: Counter = Counter()
    deleted = cons.delete_sherdog_fighters(conn, counts, apply=True)
    assert deleted == [7]
    assert any("DELETE FROM fighters WHERE id = %s" in s for s in fakedb.mutating_statements(conn))
