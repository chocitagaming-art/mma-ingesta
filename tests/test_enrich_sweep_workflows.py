"""The monthly whole-table sweeps of the two ufc.com enrichers, read from YAML.

WHY THIS FILE EXISTS. enrich-athlete-stats.yml ran its monthly ``--all`` sweep
under ``timeout-minutes: 30`` while its header promised "~19 min". The real
sweep takes 27-32 min, so GitHub cancelled it on 1-sep-2026 (2,650 of 2,885
fighters in 29.5 min, run 33488770710) and again on 1-oct-2026 (2,800 in 29.7
min). Nothing in the suite read those numbers. Here they are
written down next to the run that measured them, so the next time the table
grows or ufc.com slows down the timeout goes red in CI, not in production.

These tests read the YAML; they never run it. No network, no database.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[1]
WORKFLOWS = RAIZ / ".github" / "workflows"

# (workflow, fighters in the --all scope, worst measured seconds per fighter)
SWEEPS = [
    # 2,885 fighters in the whole-table scope on 1-oct-2026. 0.668 s/fighter:
    # run 33488770710 (1-sep-2026, 2,650 fighters in 29.52 min, then cancelled).
    # + 0.14 s: since every iteration now commits, each no-op UPDATE (~46% of
    # the scope) pays BEGIN + COMMIT round trips to Neon (2 x ~0.15 s x 0.46).
    ("enrich-athlete-stats.yml", 2885, 0.668 + 0.14),
    # 1,903 fighters with a gap. 2.048 s/fighter: run 33484806063 (1-sep-2026),
    # the slowest monthly sweep measured (fetch + delay + one Claude call).
    ("enrich-facts.yml", 1903, 2.048),
]
SETUP_MINUTES = 1  # checkout + setup-python + pip install: 11-27 s measured
MARGIN = 1.5

# The `if` that turns a scheduled run into a whole-table sweep.
SWEEP_IF = re.compile(
    r'if \[ "\$\{\{ github\.event\.schedule \}\}" = "([^"]+)" \]; then ARGS="--all"; fi'
)


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _enrich_job(datos: dict) -> dict:
    return datos["jobs"]["enrich"]


@pytest.mark.parametrize("name,fighters,seconds_per_fighter", SWEEPS)
def test_monthly_sweep_fits_in_timeout_with_margin(name, fighters, seconds_per_fighter):
    timeout = _enrich_job(_workflow(name))["timeout-minutes"]
    needed = math.ceil(fighters * seconds_per_fighter / 60 * MARGIN) + SETUP_MINUTES
    assert timeout >= needed, (
        f"{name}: timeout-minutes {timeout} < {needed} needed for {fighters} "
        f"fighters at {seconds_per_fighter:.3f} s each with a {MARGIN}x margin. "
        "Raise the timeout (or re-measure and update SWEEPS with the run id)."
    )


@pytest.mark.parametrize("name", [name for name, _, _ in SWEEPS])
def test_only_the_monthly_cron_runs_the_whole_table(name):
    datos = _workflow(name)
    # PyYAML (YAML 1.1) parses the bare key `on` as the boolean True.
    disparadores = datos.get("on", datos.get(True))
    crons = [entry["cron"] for entry in disparadores["schedule"]]

    script = next(
        step["run"]
        for step in _enrich_job(datos)["steps"]
        if "python -m src.scrapers." in step.get("run", "")
    )
    compared = SWEEP_IF.findall(script)
    assert len(compared) == 1, f"{name}: want one `--all` cron check, got {compared}"
    (monthly,) = compared

    # The string the step compares must be a cron that really exists: a typo on
    # either side silently turns the monthly sweep into a small daily run.
    assert monthly in crons, f"{name}: step compares {monthly!r}, schedule has {crons}"
    _minute, _hour, day_of_month, month, day_of_week = monthly.split()
    # Day 1 of every month. A day-of-week here would OR with the day of month
    # and sweep the whole table every week.
    assert (day_of_month, month, day_of_week) == ("1", "*", "*"), monthly

    # The other crons stay small (upcoming cards only): daily or weekly.
    others = [cron for cron in crons if cron != monthly]
    assert others, f"{name}: no small scheduled run left besides the sweep"
    assert all(cron.split()[2] == "*" for cron in others), others
