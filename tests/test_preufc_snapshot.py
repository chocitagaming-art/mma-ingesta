"""Phase 4: the read-only snapshot of the pre-UFC inputs.

fight_history_espn grows every Tuesday (espn-history.yml) and the known-fighter list
moves with each sweep, so the pre-registered CSV v2 reads both from a FROZEN snapshot:
espn_history.csv, known_fighter_ids.csv and a manifest.json with the sha256 of each
file. What these tests pin:

* load_snapshot returns EXACTLY what the database loaders return (same rows, same
  order, same dtypes, same Python types: dates as datetime.date, ids as int,
  league_id as text, None kept apart from an empty string), so a CSV generated from
  the snapshot is the CSV generated from the database at that moment;
* the files are deterministic (same data, same bytes, same sha256) and a tampered
  file is refused;
* the CLI aborts unless the session is read-only.

Pure: the fake in-memory connection stands in for Postgres. Nothing is written
outside tmp_path.
"""

from __future__ import annotations

import csv
import hashlib
import json
from contextlib import contextmanager
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from src.prediction.features import preufc_snapshot
from src.prediction.features.db import (
    ESPN_HISTORY_SQL,
    ESPN_KNOWN_FIGHTERS_SQL,
    load_espn_history_dataframe,
    load_espn_known_fighter_ids,
)
from src.prediction.features.preufc import ESPN_HISTORY_COLUMNS
from src.prediction.features.preufc_snapshot import (
    ESPN_HISTORY_FILE,
    KNOWN_IDS_FILE,
    MANIFEST_FILE,
    SnapshotError,
    load_snapshot,
    take_snapshot,
)

CREATED = datetime(2026, 10, 6, 5, 12, 3, 123456, tzinfo=timezone.utc)
UPDATED = datetime(2026, 10, 6, 5, 40, 0, tzinfo=timezone.utc)

# Unordered on purpose; every quirk the round trip must keep: a NULL date, a NULL
# method next to an EMPTY one, a NULL league, the DWCS league, a title fight, a
# result outside the CHECK list the scraper never writes (still text), accents.
ESPN_ROWS = [
    {"id": 31, "fighter_id": 7, "event_date": date(2023, 9, 12), "result": "win",
     "method": "U-DEC", "is_title_fight": False, "league_id": "3321"},
    {"id": 4, "fighter_id": 7, "event_date": date(2022, 3, 1), "result": "win",
     "method": "KO/TKO", "is_title_fight": True, "league_id": "3359"},
    {"id": 12, "fighter_id": 2, "event_date": None, "result": "loss",
     "method": None, "is_title_fight": False, "league_id": None},
    {"id": 9, "fighter_id": 2, "event_date": date(2019, 5, 1), "result": None,
     "method": "", "is_title_fight": False, "league_id": "3359"},
    {"id": 10, "fighter_id": 2, "event_date": date(2019, 5, 1), "result": "nc",
     "method": "Sumisión, \"técnica\"", "is_title_fight": False, "league_id": "3301"},
]
KNOWN_ROWS = [{"id": 7}, {"id": 2}, {"id": 40}]


def _as_the_database_orders_them(rows):
    """ESPN_HISTORY_SQL's ORDER BY fighter_id, event_date, id (NULL dates last)."""
    return sorted(
        rows,
        key=lambda r: (
            r["fighter_id"],
            r["event_date"] is None,
            r["event_date"] or date.min,
            r["id"],
        ),
    )


def _responder(espn_rows=ESPN_ROWS, known_rows=KNOWN_ROWS, read_only="on"):
    def respond(sql, params):
        normalized = " ".join(sql.split()).upper()
        if sql == ESPN_HISTORY_SQL:
            return _as_the_database_orders_them(espn_rows)
        if sql == ESPN_KNOWN_FIGHTERS_SQL:
            return list(known_rows)
        if "MAX(CREATED_AT)" in normalized and "FIGHT_HISTORY_ESPN" in normalized:
            return [{"max_created_at": CREATED, "max_updated_at": UPDATED}]
        if normalized.startswith("SHOW "):
            setting = normalized.split()[1].lower()
            return [{setting: read_only}]
        raise AssertionError(f"unexpected SQL: {normalized}")

    return respond


@pytest.fixture
def conn(fakedb):
    return fakedb.Connection(_responder())


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- take_snapshot -------------------------------------------------------------


def test_writes_the_three_files_sorted_and_the_manifest(conn, tmp_path, fakedb):
    out = tmp_path / "snap"

    manifest = take_snapshot(conn, out)

    with (out / ESPN_HISTORY_FILE).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ESPN_HISTORY_COLUMNS
    assert [int(row[0]) for row in rows[1:]] == [4, 9, 10, 12, 31]  # by id
    known_lines = (out / KNOWN_IDS_FILE).read_text(encoding="utf-8").splitlines()
    assert known_lines == ["id", "2", "7", "40"]  # sorted

    on_disk = json.loads((out / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert on_disk == manifest
    taken_at = datetime.fromisoformat(manifest["taken_at_utc"])
    assert taken_at.utcoffset().total_seconds() == 0
    assert manifest["files"][ESPN_HISTORY_FILE] == {
        "rows": 5,
        "sha256": _sha256(out / ESPN_HISTORY_FILE),
    }
    assert manifest["files"][KNOWN_IDS_FILE] == {
        "rows": 3,
        "sha256": _sha256(out / KNOWN_IDS_FILE),
    }
    assert manifest["fight_history_espn"] == {
        "max_created_at": CREATED.isoformat(),
        "max_updated_at": UPDATED.isoformat(),
    }
    assert fakedb.mutating_statements(conn) == []


def test_the_files_are_deterministic(fakedb, tmp_path):
    take_snapshot(fakedb.Connection(_responder()), tmp_path / "a")
    # Same data handed back in another order: same bytes.
    take_snapshot(
        fakedb.Connection(_responder(list(reversed(ESPN_ROWS)), KNOWN_ROWS[::-1])),
        tmp_path / "b",
    )

    for name in (ESPN_HISTORY_FILE, KNOWN_IDS_FILE):
        first, second = tmp_path / "a" / name, tmp_path / "b" / name
        assert first.read_bytes() == second.read_bytes()
    assert b"\r\n" not in (tmp_path / "a" / ESPN_HISTORY_FILE).read_bytes()


def test_an_existing_snapshot_is_never_overwritten(conn, fakedb, tmp_path):
    out = tmp_path / "snap"
    take_snapshot(conn, out)
    before = (out / ESPN_HISTORY_FILE).read_bytes()

    with pytest.raises(FileExistsError):
        take_snapshot(fakedb.Connection(_responder(ESPN_ROWS[:1])), out)

    assert (out / ESPN_HISTORY_FILE).read_bytes() == before


@pytest.mark.parametrize(
    ("column", "value"),
    [
        # A timestamp where the loader hands back a date: it would come back as
        # another type.
        ("event_date", datetime(2023, 9, 12, 20, 0)),
        ("id", 31.0),
        ("is_title_fight", 0),
        # The text that marks a NULL in the file cannot be a value too.
        ("method", r"\N"),
    ],
)
def test_a_value_that_would_not_round_trip_is_refused(fakedb, tmp_path, column, value):
    bad = [dict(ESPN_ROWS[0], **{column: value})]

    with pytest.raises(TypeError, match=column):
        take_snapshot(fakedb.Connection(_responder(bad)), tmp_path / "snap")
    assert not (tmp_path / "snap" / MANIFEST_FILE).exists()


# --- load_snapshot: the same objects as the database loaders -------------------


def test_loading_the_snapshot_equals_loading_the_database(conn, tmp_path):
    take_snapshot(conn, tmp_path / "snap")

    espn, known = load_snapshot(tmp_path / "snap")

    expected = load_espn_history_dataframe(conn)
    pd.testing.assert_frame_equal(espn, expected, check_exact=True)
    assert espn.dtypes.to_dict() == expected.dtypes.to_dict()
    assert known == load_espn_known_fighter_ids(conn)
    # Value by value, the Python types too: a missing date stays None, a missing
    # text is what pandas makes of the loader's None, and "" stays "".
    for got, want in zip(espn.to_dict("records"), expected.to_dict("records")):
        for column in ESPN_HISTORY_COLUMNS:
            assert type(got[column]) is type(want[column]), column
            if pd.isna(want[column]):
                assert pd.isna(got[column]), column
            else:
                assert got[column] == want[column], column
    by_id = espn.set_index("id")
    assert by_id.loc[9, "method"] == ""
    assert pd.isna(by_id.loc[12, "method"])
    assert by_id.loc[12, "event_date"] is None
    assert all(type(fighter_id) is int for fighter_id in known)


def test_an_empty_table_round_trips(fakedb, tmp_path):
    empty = fakedb.Connection(_responder([], []))
    take_snapshot(empty, tmp_path / "snap")

    espn, known = load_snapshot(tmp_path / "snap")

    pd.testing.assert_frame_equal(espn, load_espn_history_dataframe(empty))
    assert list(espn.columns) == ESPN_HISTORY_COLUMNS
    assert known == set()


def test_a_tampered_file_is_refused(conn, tmp_path):
    out = tmp_path / "snap"
    take_snapshot(conn, out)
    path = out / ESPN_HISTORY_FILE
    tampered = path.read_bytes().replace(b",win,", b",loss,", 1)
    assert tampered != path.read_bytes()
    path.write_bytes(tampered)

    with pytest.raises(SnapshotError, match=ESPN_HISTORY_FILE):
        load_snapshot(out)


def test_a_tampered_known_ids_file_is_refused(conn, tmp_path):
    out = tmp_path / "snap"
    take_snapshot(conn, out)
    with (out / KNOWN_IDS_FILE).open("a", encoding="utf-8", newline="") as handle:
        handle.write("41\n")

    with pytest.raises(SnapshotError, match=KNOWN_IDS_FILE):
        load_snapshot(out)


# --- CLI -----------------------------------------------------------------------------


class _Settings:
    database_url = "postgresql://do-not-use"


def _patch_cli(monkeypatch, connection):
    sessions: list[dict] = []
    connection.set_session = lambda **kwargs: sessions.append(kwargs)

    @contextmanager
    def fake_connect(url):
        assert url == _Settings.database_url
        yield connection

    monkeypatch.setattr(preufc_snapshot, "get_settings", lambda: _Settings())
    monkeypatch.setattr(preufc_snapshot, "connect", fake_connect)
    return sessions


def test_cli_aborts_when_the_session_is_not_read_only(fakedb, monkeypatch, tmp_path):
    connection = fakedb.Connection(_responder(read_only="off"))
    _patch_cli(monkeypatch, connection)

    with pytest.raises(SystemExit, match="solo lectura"):
        preufc_snapshot.main(["--out", str(tmp_path / "snap")])

    assert not (tmp_path / "snap").exists()
    assert ESPN_HISTORY_SQL not in fakedb.executed_statements(connection)


def test_cli_takes_the_snapshot_in_one_read_only_consistent_view(
    fakedb, monkeypatch, tmp_path, capsys
):
    connection = fakedb.Connection(_responder())
    sessions = _patch_cli(monkeypatch, connection)

    preufc_snapshot.main(["--out", str(tmp_path / "snap")])

    # One snapshot of the database for the three reads, set before the first one.
    assert sessions == [{"isolation_level": "REPEATABLE READ"}]
    executed = [
        " ".join(sql.split()).upper()
        for sql in fakedb.executed_statements(connection)
    ]
    assert executed[:2] == [
        "SHOW DEFAULT_TRANSACTION_READ_ONLY",
        "SHOW TRANSACTION_READ_ONLY",
    ]
    assert fakedb.mutating_statements(connection) == []
    espn, known = load_snapshot(tmp_path / "snap")
    assert len(espn) == 5 and known == {2, 7, 40}
    out = capsys.readouterr().out
    assert _sha256(tmp_path / "snap" / ESPN_HISTORY_FILE) in out
