"""CLI: build and persist the win-METHOD training dataset.

Run with ``python -m src.prediction.features.method_output``. Writes
``method_training_dataset.csv`` at the repo root (committed, like the winner
dataset) and prints a summary. Unlike the winner pipeline this does NOT mirror
the dataset into a Neon table: nothing consumes such a table, and keeping the
method pipeline read-only against the database is a feature.
"""

from __future__ import annotations

from pathlib import Path

from src.scrapers.config import get_settings

from .db import load_base_dataframe, load_rankings_dataframe
from .method_training import MethodDatasetBuildResult, build_method_training_dataset

METHOD_OUTPUT_CSV_PATH = Path("method_training_dataset.csv")


def print_summary(result: MethodDatasetBuildResult) -> None:
    dataset = result.dataset
    class_balance = (
        dataset["target"].value_counts(normalize=True).sort_index().to_dict()
        if "target" in dataset.columns
        else {}
    )
    print(f"Total fights seen: {result.total_fights_seen}")
    print(f"Total samples: {len(dataset)}")
    print(f"Class balance (0=decision, 1=ko, 2=submission): {class_balance}")
    print(
        "Exclusions:",
        {
            "no_method": result.excluded_no_method,
            "missing_history": result.excluded_missing_history,
            "missing_stats": result.excluded_missing_stats,
        },
    )


def main() -> None:
    settings = get_settings()
    fights_df = load_base_dataframe(settings.database_url)
    rankings_df = load_rankings_dataframe(settings.database_url)
    result = build_method_training_dataset(fights_df, rankings_df)
    if result.dataset.empty:
        print_summary(result)
        raise RuntimeError("No eligible method training samples were generated.")
    result.dataset.to_csv(METHOD_OUTPUT_CSV_PATH, index=False)
    print_summary(result)


if __name__ == "__main__":
    main()
