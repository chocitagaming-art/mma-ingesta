from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from src.prediction.split import partition_masks
from src.scrapers.config import get_settings
from src.scrapers.db import connect

from .dataset_guard import check_winner_dataset
from .db import (
    index_espn_history,
    load_base_dataframe,
    load_espn_history_dataframe,
    load_espn_known_fighter_ids,
    load_rankings_dataframe,
)
from .training import build_training_dataset
from .types import CORNER_SIDES, DatasetBuildResult, OUTPUT_CSV_PATH, OUTPUT_TABLE_NAME

# Partitions of the frozen metro (split.py), plus the fights after TEST_END.
COVERAGE_PARTITIONS = ("train", "calibracion", "test", "fuera")
COVERAGE_COLUMNS = ["corners", "unknown", "no_history", "with_history"]


def create_output_table(database_url: str, dataset: pd.DataFrame) -> None:
    column_definitions = []
    for column in dataset.columns:
        if column == "fight_id":
            column_definitions.append(f"{column} INTEGER")
        elif column == "event_date":
            column_definitions.append(f"{column} DATE NOT NULL")
        elif column == "target":
            column_definitions.append(f"{column} INTEGER NOT NULL")
        else:
            column_definitions.append(f"{column} DOUBLE PRECISION")
    create_table_sql = f"""
        DROP TABLE IF EXISTS {OUTPUT_TABLE_NAME};
        CREATE TABLE {OUTPUT_TABLE_NAME} (
            {", ".join(column_definitions)}
        );
    """
    with connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(create_table_sql)
            insert_columns = list(dataset.columns)
            placeholders = ", ".join(["%s"] * len(insert_columns))
            insert_sql = f"""
                INSERT INTO {OUTPUT_TABLE_NAME} ({", ".join(insert_columns)})
                VALUES ({placeholders})
            """
            rows = []
            for record in dataset.replace({np.nan: None}).to_dict("records"):
                rows.append(tuple(record[column] for column in insert_columns))
            cursor.executemany(insert_sql, rows)
        connection.commit()


def print_summary(result: DatasetBuildResult) -> None:
    dataset = result.dataset
    feature_columns = [column for column in dataset.columns if column not in {"fight_id", "event_date", "target"}]
    class_balance = (
        dataset["target"].value_counts(normalize=True).sort_index().to_dict()
        if "target" in dataset.columns
        else {}
    )
    print(f"Total fights seen: {result.total_fights_seen}")
    print(f"Total samples: {len(dataset)}")
    print(f"Feature count: {len(feature_columns)}")
    print(f"Class balance: {class_balance}")
    print(
        "Exclusions:",
        {
            "no_target": result.excluded_no_target,
            "missing_history": result.excluded_missing_history,
            "missing_stats": result.excluded_missing_stats,
        },
    )
    # Phase 4: the rows the two old gates excluded now enter with NaN history diffs.
    print(
        "Included with NaN:",
        {
            "no_ufc_history": result.included_no_ufc_history,
            "nan_stats": result.included_nan_stats,
        },
    )
    print("Spot checks:")
    for spot_check in result.spot_checks:
        print(spot_check)


def load_espn_inputs(
    database_url: str, snapshot_dir: Path | None
) -> tuple[pd.DataFrame, set[int]]:
    """fight_history_espn rows and the ids of the fighters with KNOWN ESPN history.

    From the snapshot when one is given (preufc_snapshot.py; the pre-registered CSV
    v2 is generated from one). Otherwise from the database, both in ONE connection
    so they describe the same moment."""
    if snapshot_dir is not None:
        # Imported here, not at the top: features/__init__ imports this module, and
        # `python -m src.prediction.features.preufc_snapshot` must not find the
        # snapshot module already imported (runpy would run it twice).
        from .preufc_snapshot import load_snapshot

        return load_snapshot(snapshot_dir)
    with connect(database_url) as connection:
        espn_history = load_espn_history_dataframe(connection)
        known_fighter_ids = load_espn_known_fighter_ids(connection)
    return espn_history, known_fighter_ids


def metro_partition(event_dates: pd.Series) -> pd.Series:
    """The metro partition of each date, 'fuera' after TEST_END. The boundaries
    are split.partition_masks', the same comparisons chronological_three_way_split
    makes; unlike that function it never raises: it only labels."""
    train, calibration, test = partition_masks(event_dates)
    labels = pd.Series("fuera", index=event_dates.index)
    labels[test] = "test"
    labels[calibration] = "calibracion"
    labels[train] = "train"
    return labels


def preufc_coverage(dataset: pd.DataFrame) -> pd.DataFrame:
    """Corners per metro partition by espn_has_history: unknown history (NaN: no
    espn_id or never swept), known without a prior ESPN row (0) and with history (1).

    Counts only: the target is not read."""
    partitions = metro_partition(dataset["event_date"])
    coverage = {}
    for name in COVERAGE_PARTITIONS:
        in_partition = partitions == name
        has_history = pd.concat(
            [
                dataset.loc[in_partition, f"espn_has_history_{side}"]
                for side in CORNER_SIDES
            ],
            ignore_index=True,
        )
        coverage[name] = {
            "corners": len(has_history),
            "unknown": int(has_history.isna().sum()),
            "no_history": int((has_history == 0).sum()),
            "with_history": int((has_history == 1).sum()),
        }
    return pd.DataFrame.from_dict(coverage, orient="index")[COVERAGE_COLUMNS]


def print_preufc_coverage(dataset: pd.DataFrame) -> None:
    if "espn_has_history_red" not in dataset.columns:
        print("Pre-UFC block: not in this dataset.")
        return
    print("Pre-UFC block coverage, corners per metro partition (counts only):")
    print(
        f"  {'partition':<12}{'corners':>8}{'unknown':>9}"
        f"{'no_hist':>9}{'history':>9}"
    )
    for name, row in preufc_coverage(dataset).iterrows():
        print(
            f"  {name:<12}{row['corners']:>8}{row['unknown']:>9}"
            f"{row['no_history']:>9}{row['with_history']:>9}"
        )
    print(
        "  unknown = no espn_id or never swept (the nine NaN); no_hist = known, no "
        "prior ESPN row (has_history 0); history = has_history 1"
    )


def _espn_source(snapshot_dir: Path | None, espn_rows: int, known_ids: int) -> str:
    if snapshot_dir is None:
        source = "the database (live fight_history_espn and fighters)"
    else:
        from .preufc_snapshot import (  # see above
            ESPN_HISTORY_FILE,
            KNOWN_IDS_FILE,
            read_manifest,
        )

        manifest = read_manifest(snapshot_dir)
        files = manifest["files"]
        # Both files: known_fighter_ids.csv decides every unknown-history corner.
        source = (
            f"the snapshot {snapshot_dir} (taken {manifest['taken_at_utc']}, "
            f"{ESPN_HISTORY_FILE} sha256 {files[ESPN_HISTORY_FILE]['sha256']}, "
            f"{KNOWN_IDS_FILE} sha256 {files[KNOWN_IDS_FILE]['sha256']})"
        )
    return (
        f"Pre-UFC block read from {source}: {espn_rows} ESPN rows, "
        f"{known_ids} fighters with known history."
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Genera el dataset de entrenamiento.")
    parser.add_argument(
        "--write-table",
        action="store_true",
        help=(
            "Ademas del CSV, reescribe la tabla fight_prediction_training_data en "
            "la base. Hace DROP TABLE + CREATE + INSERT sobre PRODUCCION: usar a "
            "sabiendas."
        ),
    )
    parser.add_argument(
        "--espn-snapshot",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Lee fight_history_espn y los luchadores con historial ESPN conocido de "
            "esta foto (python -m src.prediction.features.preufc_snapshot) en vez de "
            "la base. fights, stats, rankings y fisicos siguen saliendo de la base."
        ),
    )
    return parser.parse_args(argv)


def main(write_table: bool = False, espn_snapshot: Path | None = None) -> None:
    """Genera el CSV de entrenamiento.

    write_table=False por defecto A PROPOSITO: create_output_table hace DROP TABLE
    + CREATE + INSERT sobre la base de PRODUCCION, y la tabla fight_prediction_
    training_data no la lee nadie (el nombre solo aparece en features/types.py:12).
    Regenerar un CSV no puede tener ese efecto por accidente.

    espn_snapshot: carpeta de una foto de preufc_snapshot.py. Se lee PRIMERO: una
    foto que no cuadra con su manifest aborta antes de las lecturas largas.
    """
    settings = get_settings()
    espn_history, known_fighter_ids = load_espn_inputs(
        settings.database_url, espn_snapshot
    )
    fights_df = load_base_dataframe(settings.database_url)
    rankings_df = load_rankings_dataframe(settings.database_url)
    result = build_training_dataset(
        fights_df,
        rankings_df,
        espn_by_fighter=index_espn_history(espn_history),
        known_fighter_ids=known_fighter_ids,
    )
    dataset = result.dataset
    if dataset.empty:
        print_summary(result)
        raise RuntimeError("No eligible training samples were generated.")
    # ANTES de escribir: si el dataset viene envenenado (el 97 % de 2026 del CSV
    # de junio), se lanza y el CSV que hubiera se queda como estaba.
    check_winner_dataset(dataset)
    dataset.to_csv(OUTPUT_CSV_PATH, index=False)
    if write_table:
        create_output_table(settings.database_url, dataset)
        print(f"Tabla {OUTPUT_TABLE_NAME} reescrita en la base.")
    else:
        print(f"Solo CSV. Para escribir en {OUTPUT_TABLE_NAME}, usa --write-table.")
    print_summary(result)
    print(_espn_source(espn_snapshot, len(espn_history), len(known_fighter_ids)))
    print_preufc_coverage(dataset)
