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
arguments by comparing ``github.event.schedule`` with the EXACT cron string, so
a new cron silently takes whatever that branch does by default. The second
pass must be the UPCOMING-CARDS scope and nothing wider:

- enrich-photos: its default (no args) already is that scope, the same as the
  05:30 daily pass. Never the monthly ``--all`` whole-table sweep.
- backfill-standing: its default is NOT. The scheduled branch hands every cron
  ``--limit 40``, i.e. the whole ufc.com catalogue (9 upcoming + 31 completed
  events on 22-oct-2026, exactly 40). Run 39777373231 (06:40 cron, 22-oct):
  40 event pages, 1 x 403 on ufc-176, 175 legacy UPDATEs and 0 directional
  ones, all of them older cards overwriting the newer photo (BACKLOG: "deja la
  foto de pie MAS VIEJA"). So the evening pass needs ``--upcoming`` (status =
  'upcoming', no deduced-slug round), selected by its exact cron string, while
  the daily and the weekly crons keep their ``--limit 40``.

These tests read the YAML and the CLI parser; they never run a pass. No
network, no database.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

from src.scrapers import backfill_standing_photos

RAIZ = Path(__file__).resolve().parents[1]
WORKFLOWS = RAIZ / ".github" / "workflows"

# (workflow, job, its daily cron, the ARGS its Thursday/Friday evening pass
# must run with). Both are the upcoming-cards scope of that scraper.
FIGHT_WEEK = [
    ("enrich-photos.yml", "enrich", "30 5 * * *", ""),
    ("backfill-standing.yml", "backfill", "40 6 * * *", "--upcoming"),
]
# backfill-standing's catalogue sweeps, which the evening check must not touch.
STANDING_CATALOGUE_CRONS = ("40 6 * * *", "0 7 * * 1")
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


@pytest.mark.parametrize("name,job,daily,expected", FIGHT_WEEK)
def test_fight_week_pass_runs_thursday_and_friday_evening(name, job, daily, expected):
    passes = _fight_week_crons(_crons(_workflow(name)))
    covered = set().union(*(_days_of_week(cron.split()[4]) for cron in passes))
    missing = {THURSDAY, FRIDAY} - covered
    assert not missing, (
        f"{name}: no evening pass after {BATCH_DONE_UTC[0]}:{BATCH_DONE_UTC[1]:02d}Z "
        f"on day(s) {sorted(missing)} (4 = Thursday, 5 = Friday); crons with a fixed "
        f"evening time: {passes}. The fight-week photos would wait for the next "
        "morning's cron, which GitHub starts hours late."
    )


@pytest.mark.parametrize("name,job,daily,expected", FIGHT_WEEK)
def test_fight_week_pass_is_the_upcoming_cards_scope(name, job, daily, expected):
    datos = _workflow(name)
    crons = _crons(datos)
    assert daily in crons, f"{name}: daily cron {daily!r} is gone; schedule: {crons}"
    passes = _fight_week_crons(crons)
    assert passes, f"{name}: no Thursday/Friday evening pass in {crons}"

    script = _script(datos, job)
    for cron in passes:
        args = _scheduled_args(script, cron)
        assert args == expected, (
            f"{name}: the evening pass {cron!r} runs with {args!r}, it must run "
            f"with {expected!r} (upcoming cards only)."
        )
        assert "--all" not in args.split(), f"{name}: {cron!r} would sweep the whole table"
        # A fixed cap is not a scope: newest-first, the future cards eat it and
        # it silently stops reaching what it was meant for (backfill-standing's
        # --limit 8 of 2026-08-01, its --limit 12 of 2026-08-28).
        assert "--limit" not in args.split(), f"{name}: {cron!r} scopes by a fixed --limit"


def test_backfill_standing_catalogue_crons_keep_their_sweep():
    """The evening check is an exact-string match: it must not leak into the
    daily and weekly catalogue sweeps, which still need every ufc.com event."""
    datos = _workflow("backfill-standing.yml")
    crons = _crons(datos)
    script = _script(datos, "backfill")
    for cron in STANDING_CATALOGUE_CRONS:
        assert cron in crons, f"catalogue cron {cron!r} is gone; schedule: {crons}"
        assert _scheduled_args(script, cron) == "--limit 40", cron


def test_backfill_standing_cli_accepts_every_scheduled_args():
    """What the YAML passes must parse: an unknown flag would only show up as
    an argparse exit 2 on a Thursday night, in the GitHub run list."""
    datos = _workflow("backfill-standing.yml")
    script = _script(datos, "backfill")
    parser = backfill_standing_photos.build_parser()
    for cron in _crons(datos):
        args = _scheduled_args(script, cron)
        parsed = parser.parse_args(shlex.split(args))
        assert parsed.dry_run is False, cron
    evening = parser.parse_args(shlex.split(_scheduled_args(script, "40 20 * * 4,5")))
    assert evening.upcoming is True
    assert evening.limit is None
