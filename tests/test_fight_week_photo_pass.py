"""The fight-week second pass of the photo crons, read from YAML.

WHY THIS FILE EXISTS. ufc.com uploads the fight-week photos (the batch named
``*_MM-DD.png`` after the event date) on THURSDAY evening, after every
scheduled photo pass of the day has run. Measured on 22-oct-2026 from the S3
Last-Modified of the originals, 13 cards from 1-aug to 24-oct-2026: the first
file landed on Thursday every week, between 16:19Z and 22:20Z, and the batch
was complete by 19:51Z on 12 of the 13 (22-aug: 22:20-23:28Z). GitHub starts
the daily crons hours late (since 8-oct, 05:30 fired at 09:36-13:26Z and 06:40
at 11:08-16:38Z), but never late enough: a debutant created from ESPN with no
ESPN photo kept the silhouette until Friday afternoon. Until this pass existed
it was patched by hand every Thursday night: on 22-oct the UFC 333 batch
landed at 16:19-16:24Z and only the manual dispatch at 19:53Z (run
39844015851) gave Asaf Chopurov his face.

The trap these tests close: the scheduled branch of each step picks the
arguments, enrich-photos by comparing ``github.event.schedule`` with the EXACT
cron string, so a new cron silently takes whatever that branch does by
default. Here it must be the daily pass's scope (upcoming cards for
enrich-photos, ``--limit 40`` for backfill-standing), never the monthly
``--all`` whole-table sweep.

These tests read the YAML; they never run it. No network, no database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[1]
WORKFLOWS = RAIZ / ".github" / "workflows"

# (workflow, job, its daily cron). The second pass must get exactly the scope
# of that daily cron.
FIGHT_WEEK = [
    ("enrich-photos.yml", "enrich", "30 5 * * *"),
    ("backfill-standing.yml", "backfill", "40 6 * * *"),
]
THURSDAY, FRIDAY = 4, 5
# Latest time the Thursday batch was complete on 12 of 13 cards (22-oct-2026).
# A pass scheduled before it would mostly find the old photos.
BATCH_DONE_UTC = (19, 51)

SCHEDULE_BRANCH = re.compile(
    r'if \[ "\$\{\{ github\.event_name \}\}" = "schedule" \]; then\n(.*?)\n\s*else\n',
    re.S,
)
PLAIN_ARGS = re.compile(r'ARGS="([^"]*)"(?:\s+#.*)?')
CRON_CHECK = re.compile(
    r'if \[ "\$\{\{ github\.event\.schedule \}\}" = "([^"]+)" \]; '
    r'then ARGS="([^"]*)"; fi'
)


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _crons(datos: dict) -> list[str]:
    # PyYAML (YAML 1.1) parses the bare key `on` as the boolean True.
    disparadores = datos.get("on", datos.get(True))
    return [entry["cron"] for entry in disparadores["schedule"]]


def _script(datos: dict, job: str) -> str:
    return next(
        step["run"]
        for step in datos["jobs"][job]["steps"]
        if "python -m src.scrapers." in step.get("run", "")
    )


def _days_of_week(field: str) -> set[int]:
    """Days (0 = Sunday) a cron day-of-week field fires on. Plain numbers,
    lists and ranges only: anything fancier fails here instead of passing."""
    if field == "*":
        return set(range(7))
    days: set[int] = set()
    for part in field.split(","):
        first, _, last = part.partition("-")
        days.update(day % 7 for day in range(int(first), int(last or first) + 1))
    return days


def _fight_week_crons(crons: list[str]) -> list[str]:
    """Crons that fire on Thursday or Friday at a fixed time after the batch."""
    passes = []
    for cron in crons:
        minute, hour, day_of_month, month, day_of_week = cron.split()
        if not (minute.isdigit() and hour.isdigit()):
            continue
        if (day_of_month, month) != ("*", "*") or day_of_week == "*":
            continue
        if not _days_of_week(day_of_week) & {THURSDAY, FRIDAY}:
            continue
        if (int(hour), int(minute)) > BATCH_DONE_UTC:
            passes.append(cron)
    return passes


def _scheduled_args(script: str, cron: str) -> str:
    """The ARGS a scheduled run of `cron` ends up with, read off the step.

    Understands exactly the two shapes these workflows use: a plain
    ``ARGS="..."`` and ``if [ "${{ github.event.schedule }}" = "<cron>" ];
    then ARGS="..."; fi``. Anything else in the scheduled branch fails the
    test: better a red that asks for a human than a green that guessed.
    """
    lines = [line.strip() for line in script.splitlines() if line.strip()]
    assert lines[0] == 'ARGS=""', f'the step does not start with ARGS="": {lines[0]!r}'
    assert any(
        line.startswith("python -m src.scrapers.") and line.endswith("$ARGS")
        for line in lines
    ), "the python command no longer takes $ARGS"
    (branch,) = SCHEDULE_BRANCH.findall(script)
    args = ""
    for raw in branch.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        plain = PLAIN_ARGS.fullmatch(line)
        if plain:
            args = plain.group(1)
            continue
        check = CRON_CHECK.fullmatch(line)
        if check:
            if check.group(1) == cron:
                args = check.group(2)
            continue
        raise AssertionError(
            f"unknown statement in the scheduled branch: {line!r}. This test "
            "cannot tell which scope a cron gets any more: check it by hand."
        )
    return args.strip()


@pytest.mark.parametrize("name,job,daily", FIGHT_WEEK)
def test_fight_week_pass_runs_thursday_and_friday_evening(name, job, daily):
    passes = _fight_week_crons(_crons(_workflow(name)))
    covered = set().union(*(_days_of_week(cron.split()[4]) for cron in passes))
    missing = {THURSDAY, FRIDAY} - covered
    assert not missing, (
        f"{name}: no evening pass after {BATCH_DONE_UTC[0]}:{BATCH_DONE_UTC[1]:02d}Z "
        f"on day(s) {sorted(missing)} (4 = Thursday, 5 = Friday); crons with a fixed "
        f"evening time: {passes}. The fight-week photos would wait for the next "
        "morning's cron, which GitHub starts hours late."
    )


@pytest.mark.parametrize("name,job,daily", FIGHT_WEEK)
def test_fight_week_pass_keeps_the_daily_scope(name, job, daily):
    datos = _workflow(name)
    crons = _crons(datos)
    assert daily in crons, f"{name}: daily cron {daily!r} is gone; schedule: {crons}"
    passes = _fight_week_crons(crons)
    assert passes, f"{name}: no Thursday/Friday evening pass in {crons}"

    script = _script(datos, job)
    daily_args = _scheduled_args(script, daily)
    for cron in passes:
        args = _scheduled_args(script, cron)
        assert args == daily_args, (
            f"{name}: the second pass {cron!r} runs with {args!r}, the daily "
            f"{daily!r} with {daily_args!r}. It must be the same small pass."
        )
        assert "--all" not in args.split(), (
            f"{name}: {cron!r} would sweep the whole table"
        )
