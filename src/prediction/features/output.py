from __future__ import annotations

import numpy as np
import pandas as pd

from src.scrapers.config import get_settings
from src.scrapers.db import connect

from .db import load_base_dataframe, load_rankings_dataframe
from .training import build_training_dataset
from .types import DatasetBuildResult, OUTPUT_CSV_PATH, OUTPUT_TABLE_NAME


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
    print("Spot checks:")
    for spot_check in result.spot_checks:
        print(spot_check)


def main(write_table: bool = False) -> None:
    """Genera el CSV de entrenamiento.

    write_table=False por defecto A PROPOSITO: create_output_table hace DROP TABLE
    + CREATE + INSERT sobre la base de PRODUCCION, y la tabla fight_prediction_
    training_data no la lee nadie (el nombre solo aparece en features/types.py:12).
    Regenerar un CSV no puede tener ese efecto por accidente.
    """
    settings = get_settings()
    fights_df = load_base_dataframe(settings.database_url)
    rankings_df = load_rankings_dataframe(settings.database_url)
    result = build_training_dataset(fights_df, rankings_df)
    dataset = result.dataset
    if dataset.empty:
        print_summary(result)
        raise RuntimeError("No eligible training samples were generated.")
    dataset.to_csv(OUTPUT_CSV_PATH, index=False)
    if write_table:
        create_output_table(settings.database_url, dataset)
        print(f"Tabla {OUTPUT_TABLE_NAME} reescrita en la base.")
    else:
        print(f"Solo CSV. Para escribir en {OUTPUT_TABLE_NAME}, usa --write-table.")
    print_summary(result)
