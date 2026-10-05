"""keepalive-prediction.yml goes red when the pre-UFC block is being served degraded.

Phase 4: if fight_history_espn fails to load, the service still answers 200 and
serves every corner's block as "unknown" (see service._get_dataframes). /health
says so in ``preUfc.state`` and the keep-alive, which already polls /health, turns
that into a failed run (and notify-on-failure into an Issue). Nobody reads /health
by hand, so without this the degradation would be silent.

The check runs on every pass (it reads the body the ping already fetched, from the
service's memory, at no cost to Neon), AFTER the heartbeat step, so the panel still
records that the service answered. The cadence does not change.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "keepalive-prediction.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    return _workflow()["jobs"]["ping"]["steps"]


def _check_step() -> dict:
    matches = [step for step in _steps() if "preUfc" in step.get("run", "")]
    assert len(matches) == 1, "exactly one step must check preUfc"
    return matches[0]


def _check_script() -> str:
    """The Python the check step feeds to python3 through a quoted heredoc."""
    match = re.search(r"<<'PY'\n(.*?)\nPY\b", _check_step()["run"], re.DOTALL)
    assert match, "the check is a python3 heredoc (<<'PY' ... PY)"
    return match.group(1)


def test_cadence_is_unchanged():
    workflow = _workflow()
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert [entry["cron"] for entry in triggers["schedule"]] == [
        "*/10 * * * *",
        "5 * * * *",
    ]


def test_ping_keeps_the_body_without_a_pipe():
    """The body is written to a file by curl itself: a pipe would hand the step
    the exit code of the last command and hide a failed curl."""
    ping = _steps()[0]["run"]
    assert "curl -fsS" in ping
    assert '-o "$RUNNER_TEMP/health.json"' in ping
    assert "|" not in ping.replace("||", "")


def test_check_runs_after_the_heartbeat():
    names = [step["name"] for step in _steps()]
    assert names.index(_check_step()["name"]) > names.index("Anotar el latido")
    assert "if" not in _check_step(), "it runs on every pass, shallow and deep"
    assert '"$RUNNER_TEMP/health.json"' in _check_step()["run"]


@pytest.mark.parametrize(
    ("body", "exit_code"),
    [
        ({"status": "ok", "preUfc": {"needed": True, "state": "unavailable"}}, 1),
        ({"status": "ok", "preUfc": {"needed": True, "state": "ok"}}, 0),
        ({"status": "ok", "preUfc": {"needed": True, "state": "not_loaded"}}, 0),
        ({"status": "ok", "preUfc": {"needed": False, "state": "not_needed"}}, 0),
        # A service from before phase 4: no preUfc at all.
        ({"status": "ok", "db": "skipped"}, 0),
        ("<html>not json</html>", 0),
    ],
    ids=["unavailable", "ok", "not-loaded", "not-needed", "old-service", "not-json"],
)
def test_check_fails_only_when_the_block_is_needed_and_unavailable(
    tmp_path, body, exit_code
):
    health = tmp_path / "health.json"
    text = body if isinstance(body, str) else json.dumps(body)
    health.write_text(text, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", _check_script(), str(health)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == exit_code, result.stdout + result.stderr
    if exit_code:
        assert "::error::" in result.stdout
