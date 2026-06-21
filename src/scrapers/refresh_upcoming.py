"""Orchestrate the upcoming-event refresh pipeline end to end.

Keeping the live site current when cards are announced or dates pass takes five
separate scrapers, run in a fixed order. This module chains them so a single
command does the whole refresh:

  1. upcoming_events     - re-scrape ufc.com/events (events + bouts; complete past ones)
  2. link_upcoming       - create + link name-only bout fighters from ESPN
  3. enrich_upcoming     - ESPN photo / nationality / measures for upcoming gaps
  4. enrich_records_espn - fill any remaining 0-0-0 records from ESPN
  5. backfill_results    - fill winner/method/round onto bouts of events that just finished

Each step writes on its own DB connection and commits before the next starts, so
ordering dependencies (step 2 needs step 1's events; steps 3-5 need step 2's
fighters) hold. Steps are independent failures: if one raises, it is logged and
the pipeline continues — later steps still maintain existing data. A combined
per-step summary is printed at the end and the process exits non-zero if any
step failed.

This is intentionally a separate module rather than wiring enrichment into
upcoming_events.py: each scraper stays single-purpose and runnable in isolation.

Run whenever upcoming events change (new cards, passed dates):
    python -m src.scrapers.refresh_upcoming --dry-run        # full pipeline, no writes
    python -m src.scrapers.refresh_upcoming                  # writes to DB
    python -m src.scrapers.refresh_upcoming --records-limit 100
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .backfill_results import backfill
from .enrich_records_espn import enrich_records
from .enrich_upcoming import enrich_upcoming_fighters
from .link_upcoming_fighters import link_upcoming
from .logging_config import configure_logging
from .upcoming_events import scrape_upcoming_events

LOGGER = logging.getLogger(__name__)


def _run_step(name: str, step) -> dict:
    """Run one pipeline step, capturing its counts (or the error) without aborting."""
    LOGGER.info("=== STEP %s START ===", name)
    started = time.monotonic()
    try:
        result = step()
        elapsed = round(time.monotonic() - started, 1)
        counts = dict(result)  # Counter and plain dict both normalize cleanly
        LOGGER.info("=== STEP %s OK (%ss) === %s", name, elapsed, json.dumps(counts, ensure_ascii=False))
        return {"status": "ok", "elapsed_s": elapsed, "counts": counts}
    except Exception as exc:  # noqa: BLE001 - one bad step must not kill the pipeline
        elapsed = round(time.monotonic() - started, 1)
        LOGGER.exception("=== STEP %s FAILED (%ss) ===", name, elapsed)
        return {"status": "failed", "elapsed_s": elapsed, "error": f"{type(exc).__name__}: {exc}"}


def refresh(dry_run: bool = False, records_limit: int | None = None) -> dict:
    steps = [
        ("upcoming_events", lambda: scrape_upcoming_events(dry_run=dry_run)),
        ("link_upcoming", lambda: link_upcoming(dry_run=dry_run)),
        ("enrich_upcoming", lambda: enrich_upcoming_fighters(dry_run=dry_run)),
        ("enrich_records_espn", lambda: enrich_records(dry_run=dry_run, limit=records_limit)),
        ("backfill_results", lambda: backfill(dry_run=dry_run)),
    ]
    summary: dict[str, dict] = {}
    for name, step in steps:
        summary[name] = _run_step(name, step)
    return summary


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Run the full upcoming-event refresh pipeline.")
    parser.add_argument("--dry-run", action="store_true", help="Run every step in report-only mode (no writes).")
    parser.add_argument(
        "--records-limit",
        type=int,
        default=None,
        help="Cap how many 0-0-0 fighters the ESPN records step processes.",
    )
    args = parser.parse_args()

    summary = refresh(dry_run=args.dry_run, records_limit=args.records_limit)
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    failed = [name for name, info in summary.items() if info.get("status") != "ok"]
    if failed:
        LOGGER.error("Pipeline finished with failed steps: %s", ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
