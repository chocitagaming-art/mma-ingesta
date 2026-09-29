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
step failed OR raised an alarm (ALARM_COUNTERS / ALARM_IF_ZERO): a step that
swallows its errors into a counter must still turn the run red.

This is intentionally a separate module rather than wiring enrichment into
upcoming_events.py: each scraper stays single-purpose and runnable in isolation.

Run whenever upcoming events change (new cards, passed dates):
    python -m src.scrapers.refresh_upcoming --dry-run        # full pipeline, no writes
    python -m src.scrapers.refresh_upcoming                  # writes to DB
    python -m src.scrapers.refresh_upcoming --records-limit 100
    python -m src.scrapers.refresh_upcoming --forzar-evento ufc-332   # see --help
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Sequence

from .backfill_results import backfill
from .enrich_records_espn import enrich_records
from .enrich_upcoming import enrich_upcoming_fighters
from .link_upcoming_fighters import link_upcoming
from .logging_config import configure_logging
from .upcoming_events import scrape_upcoming_events

LOGGER = logging.getLogger(__name__)

# Counters that mean "this step did NOT do its job" although it returned
# normally: each one stands for an exception the step swallowed, or an event it
# chose not to touch. Any of them above 0 turns the run red, AFTER every step
# has run, so notify-on-failure opens its Issue. Until 29-sep-2026 they were
# ignored: a failed ufc.com detail page cancelled a whole card, run in green.
#
# NOT alarms, on purpose: backfill_results.events_unmatched is 1 EVERY day (the
# Road To UFC, event 1094, is not on ufcstats) and would keep the cron red for
# good; neither are bouts_unmatched, stats_unmatched or the link/enrich counts.
ALARM_COUNTERS: dict[str, tuple[str, ...]] = {
    "upcoming_events": (
        "write_errors",
        "detail_errors",
        "listing_pages_failed",
        "cards_guarded",
        "events_skipped_detail",
    ),
    "backfill_results": ("event_errors",),
}

# The other way round: counters that must NOT be 0. An empty ufc.com listing
# (anti-bot page, or page 0 served empty) used to end green having done nothing.
ALARM_IF_ZERO: dict[str, tuple[str, ...]] = {
    "upcoming_events": ("events_found",),
}


def _run_step(name: str, step) -> dict:
    """Run one pipeline step, capturing its counts (or the error) without aborting."""
    LOGGER.info("=== STEP %s START ===", name)
    started = time.monotonic()
    try:
        result = step()
        elapsed = round(time.monotonic() - started, 1)
        counts = dict(result)  # Counter and plain dict both normalize cleanly
        # Alarm counters always show, at 0 too: a Counter only holds the keys
        # that were incremented, so `write_errors: 0` never reached the log and
        # "no errors" could not be told apart from "not reported".
        for key in ALARM_COUNTERS.get(name, ()):
            counts.setdefault(key, 0)
        LOGGER.info("=== STEP %s OK (%ss) === %s", name, elapsed, json.dumps(counts, ensure_ascii=False))
        return {"status": "ok", "elapsed_s": elapsed, "counts": counts}
    except Exception as exc:  # noqa: BLE001 - one bad step must not kill the pipeline
        elapsed = round(time.monotonic() - started, 1)
        LOGGER.exception("=== STEP %s FAILED (%ss) ===", name, elapsed)
        return {"status": "failed", "elapsed_s": elapsed, "error": f"{type(exc).__name__}: {exc}"}


def _alarms(summary: dict) -> list[str]:
    """The alarms raised by the steps that ran, as 'step.counter=value'.

    A failed step has no counts and already fails the run on its own, so it is
    skipped here.
    """
    alarms: list[str] = []
    for name, info in summary.items():
        if info.get("status") != "ok":
            continue
        counts = info.get("counts", {})
        for key in ALARM_COUNTERS.get(name, ()):
            if counts.get(key, 0) > 0:
                alarms.append(f"{name}.{key}={counts[key]}")
        for key in ALARM_IF_ZERO.get(name, ()):
            if counts.get(key, 0) == 0:
                alarms.append(f"{name}.{key}=0")
    return alarms


def refresh(
    dry_run: bool = False,
    records_limit: int | None = None,
    forzar_eventos: Sequence[str] = (),
) -> dict:
    steps = [
        (
            "upcoming_events",
            lambda: scrape_upcoming_events(dry_run=dry_run, forzar_eventos=forzar_eventos),
        ),
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
    parser.add_argument(
        "--forzar-evento",
        action="append",
        default=[],
        type=str.strip,
        metavar="SLUG",
        help=(
            "Apply a REAL card drop that the upcoming_events guard is holding back "
            "(cards_guarded), e.g. ufc-332. Skips only the card-size guard and only "
            "for that slug; an event whose detail page failed is never written. "
            "Repeatable."
        ),
    )
    args = parser.parse_args()

    summary = refresh(
        dry_run=args.dry_run,
        records_limit=args.records_limit,
        forzar_eventos=args.forzar_evento,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    # Every step has run by now: the red only reports, it never skips work.
    failed = [name for name, info in summary.items() if info.get("status") != "ok"]
    alarms = _alarms(summary)
    if failed:
        LOGGER.error("Pipeline finished with failed steps: %s", ", ".join(failed))
    if alarms:
        LOGGER.error("Pipeline finished with alarms: %s", ", ".join(alarms))
    if failed or alarms:
        sys.exit(1)


if __name__ == "__main__":
    main()
