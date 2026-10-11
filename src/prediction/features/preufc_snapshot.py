"""Read-only snapshot of the pre-UFC inputs of the winner CSV (phase 4).

fight_history_espn grows every Tuesday (espn-history.yml) and the list of fighters
with KNOWN ESPN history moves with each sweep, so the pre-registered CSV v2 reads both
from a frozen snapshot instead of the live tables (``python -m src.prediction.features
--espn-snapshot DIR``). A snapshot is a directory with three files:

    espn_history.csv        ESPN_HISTORY_COLUMNS, one line per fight_history_espn row,
                            sorted by id
    known_fighter_ids.csv   the ids of db.load_espn_known_fighter_ids, sorted
    manifest.json           UTC time, rows and sha256 of each file, and MAX(created_at)
                            / MAX(updated_at) of fight_history_espn

load_snapshot checks both sha256 against the manifest and returns EXACTLY what the
database loaders return: the frame of db.load_espn_history_dataframe (same rows, same
order, same dtypes: dates as datetime.date, ids as int, league_id as text, an empty
string kept apart from a NULL) and the set of db.load_espn_known_fighter_ids. So a CSV
generated from a snapshot is the CSV generated from the database when it was taken.

Only these two inputs are frozen: fights, stats, rankings and physicals are still read
live by the generator. That is why the artifact the measurement keeps is the CSV itself,
with its sha256.

Take one (aborts unless the session is read-only):
    PGOPTIONS='-c default_transaction_read_only=on' \\
        python -m src.prediction.features.preufc_snapshot --out DIR
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from psycopg2.extensions import connection as PgConnection

from src.scrapers.config import get_settings
from src.scrapers.db import connect, cursor

from .db import (
    ESPN_HISTORY_SQL,
    ESPN_KNOWN_FIGHTERS_SQL,
    fetch_espn_history_rows,
    load_espn_known_fighter_ids,
)
from .preufc import ESPN_HISTORY_COLUMNS

ESPN_HISTORY_FILE = "espn_history.csv"
KNOWN_IDS_FILE = "known_fighter_ids.csv"
MANIFEST_FILE = "manifest.json"
SNAPSHOT_FORMAT = 1

ESPN_HISTORY_STAMPS_SQL = """
    SELECT MAX(created_at) AS max_created_at, MAX(updated_at) AS max_updated_at
    FROM fight_history_espn
"""
READ_ONLY_SETTINGS = ("default_transaction_read_only", "transaction_read_only")

# A NULL cell, as PostgreSQL's COPY writes it. The stdlib csv reader cannot tell an
# unquoted empty field from a quoted one, so an empty field would not say whether the
# value was NULL or ''. A text value equal to the marker is refused instead.
NULL_MARKER = "\\N"

# Type each column has in the rows psycopg2 hands back (INTEGER -> int, DATE ->
# datetime.date, TEXT -> str, BOOLEAN -> bool), or None. Anything else would not come
# back as the same object, so take_snapshot refuses it (a datetime is not a date, a
# bool is not an int).
_COLUMN_TYPES: dict[str, type] = {
    "id": int,
    "fighter_id": int,
    "event_date": date,
    "result": str,
    "method": str,
    "is_title_fight": bool,
    "league_id": str,
}
_BOOLEAN_TEXT = {"True": True, "False": False}


class SnapshotError(RuntimeError):
    """The snapshot on disk is not the one its manifest describes: do not use it."""


def _encode(column: str, value: Any) -> str:
    if value is None:
        return NULL_MARKER
    expected = _COLUMN_TYPES[column]
    if type(value) is not expected or value == NULL_MARKER:
        raise TypeError(
            f"fight_history_espn.{column} = {value!r} ({type(value).__name__}): the "
            f"snapshot keeps {expected.__name__} or NULL only, so it could not hand "
            "this value back unchanged."
        )
    return value.isoformat() if expected is date else str(value)


def _decode(column: str, text: str) -> Any:
    if text == NULL_MARKER:
        return None
    expected = _COLUMN_TYPES[column]
    if expected is int:
        return int(text)
    if expected is date:
        return date.fromisoformat(text)
    if expected is bool:
        return _BOOLEAN_TEXT[text]
    return text


def _loader_order(record: dict[str, Any]) -> tuple:
    """ESPN_HISTORY_SQL's ORDER BY fighter_id, event_date, id (PostgreSQL puts a
    NULL date last in ascending order)."""
    event_date = record["event_date"]
    return (
        record["fighter_id"],
        event_date is None,
        event_date or date.min,
        record["id"],
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _single_line(sql: str) -> str:
    return " ".join(sql.split())


def take_snapshot(connection: PgConnection, out_dir: Path | str) -> dict[str, Any]:
    """Write the snapshot of the pre-UFC inputs into ``out_dir``; return its manifest.

    Reads in the given connection only (SELECTs); the caller makes it read-only and
    one consistent view (main does both). Never overwrites: an existing snapshot in
    ``out_dir`` raises FileExistsError before anything is written. Same data, same
    bytes: the files do not depend on the order the rows arrive in."""
    out_dir = Path(out_dir)
    names = (ESPN_HISTORY_FILE, KNOWN_IDS_FILE, MANIFEST_FILE)
    paths = {name: out_dir / name for name in names}
    existing = [str(path) for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(f"There is already a snapshot here: {existing}")

    rows = fetch_espn_history_rows(connection)
    known_ids = load_espn_known_fighter_ids(connection)
    with cursor(connection) as db_cursor:
        db_cursor.execute(ESPN_HISTORY_STAMPS_SQL)
        stamps = db_cursor.fetchone()

    # Encode everything first: a value that would not round trip leaves no file.
    lines = [
        [_encode(column, row[column]) for column in ESPN_HISTORY_COLUMNS]
        for row in sorted(rows, key=lambda row: row["id"])
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    with paths[ESPN_HISTORY_FILE].open("x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(ESPN_HISTORY_COLUMNS)
        writer.writerows(lines)
    with paths[KNOWN_IDS_FILE].open("x", encoding="utf-8", newline="") as handle:
        handle.write("id\n")
        handle.writelines(f"{fighter_id}\n" for fighter_id in sorted(known_ids))

    manifest = {
        "format": SNAPSHOT_FORMAT,
        "taken_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fight_history_espn": {
            "max_created_at": _iso_or_none(stamps["max_created_at"]),
            "max_updated_at": _iso_or_none(stamps["max_updated_at"]),
        },
        "files": {
            ESPN_HISTORY_FILE: {
                "rows": len(lines),
                "sha256": _sha256(paths[ESPN_HISTORY_FILE]),
            },
            KNOWN_IDS_FILE: {
                "rows": len(known_ids),
                "sha256": _sha256(paths[KNOWN_IDS_FILE]),
            },
        },
        "sql": {
            "espn_history": _single_line(ESPN_HISTORY_SQL),
            "known_fighter_ids": _single_line(ESPN_KNOWN_FIGHTERS_SQL),
        },
    }
    with paths[MANIFEST_FILE].open("x", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def read_manifest(directory: Path | str) -> dict[str, Any]:
    return json.loads((Path(directory) / MANIFEST_FILE).read_text(encoding="utf-8"))


def load_snapshot(directory: Path | str) -> tuple[pd.DataFrame, set[int]]:
    """(espn_history_frame, known_fighter_ids) of a snapshot, after checking that both
    files are the ones its manifest describes (sha256 and rows).

    The frame equals db.load_espn_history_dataframe at the moment the snapshot was
    taken (same order and dtypes); the set equals db.load_espn_known_fighter_ids."""
    directory = Path(directory)
    files = read_manifest(directory)["files"]
    for name in (ESPN_HISTORY_FILE, KNOWN_IDS_FILE):
        actual = _sha256(directory / name)
        if actual != files[name]["sha256"]:
            raise SnapshotError(
                f"{directory / name}: sha256 {actual} does not match the manifest "
                f"({files[name]['sha256']}). The file changed after the snapshot "
                "was taken: do not use it."
            )

    with (directory / ESPN_HISTORY_FILE).open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        if header != ESPN_HISTORY_COLUMNS:
            raise SnapshotError(f"{ESPN_HISTORY_FILE}: unexpected header {header}")
        records = [
            {
                column: _decode(column, text)
                for column, text in zip(header, line, strict=True)
            }
            for line in reader
        ]
    known_lines = (directory / KNOWN_IDS_FILE).read_text(encoding="utf-8").splitlines()
    if known_lines[:1] != ["id"]:
        raise SnapshotError(f"{KNOWN_IDS_FILE}: unexpected header {known_lines[:1]}")
    known_ids = {int(line) for line in known_lines[1:]}

    row_counts = {ESPN_HISTORY_FILE: len(records), KNOWN_IDS_FILE: len(known_ids)}
    for name, rows in row_counts.items():
        if rows != files[name]["rows"]:
            raise SnapshotError(
                f"{name}: {rows} rows, the manifest says {files[name]['rows']}."
            )
    records.sort(key=_loader_order)
    return pd.DataFrame(records, columns=ESPN_HISTORY_COLUMNS), known_ids


def require_read_only(connection: PgConnection) -> None:
    """SystemExit unless both read-only settings of the session are on."""
    with cursor(connection) as db_cursor:
        for setting in READ_ONLY_SETTINGS:
            db_cursor.execute(f"SHOW {setting}")
            value = db_cursor.fetchone()[setting]
            if value != "on":
                raise SystemExit(
                    f"La sesion NO es de solo lectura ({setting} = {value}): no "
                    "se toma la foto. Exporta "
                    "PGOPTIONS='-c default_transaction_read_only=on'."
                )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Foto de solo lectura de fight_history_espn y de los luchadores con "
            "historial ESPN conocido (fase 4)."
        )
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Carpeta de la foto (si ya hay una dentro, no se sobrescribe).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = get_settings()
    with connect(settings.database_url) as connection:
        # One snapshot of the database for every read below. Set before the first
        # statement: a transaction cannot change its isolation level once started.
        connection.set_session(isolation_level="REPEATABLE READ")
        require_read_only(connection)
        manifest = take_snapshot(connection, args.out)
    print(f"Foto de ESPN escrita en {args.out} ({manifest['taken_at_utc']}).")
    for name, entry in manifest["files"].items():
        print(f"  {name}: {entry['rows']} filas, sha256 {entry['sha256']}")
    stamps = manifest["fight_history_espn"]
    print(
        f"  fight_history_espn: MAX(created_at) {stamps['max_created_at']}, "
        f"MAX(updated_at) {stamps['max_updated_at']}"
    )


if __name__ == "__main__":
    main()
