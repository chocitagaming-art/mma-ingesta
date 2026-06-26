import os
from datetime import datetime, timezone

from src.prediction.api import model_trained_at


def test_prefers_stamped_trained_at():
    # When the bundle carries a trained_at, that wins (and the file is ignored).
    assert model_trained_at({"trained_at": "2026-06-25"}) == "2026-06-25"


def test_falls_back_to_file_mtime_when_not_stamped(tmp_path):
    f = tmp_path / "model.joblib"
    f.write_bytes(b"x")
    # Known mtime: 2026-01-02 12:00:00 UTC -> date 2026-01-02 (noon avoids any
    # sub-second rounding shifting the day).
    ts = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(f, (ts, ts))
    assert model_trained_at({}, f) == "2026-01-02"


def test_returns_none_when_no_stamp_and_no_file(tmp_path):
    missing = tmp_path / "nope.joblib"
    assert model_trained_at({}, missing) is None
