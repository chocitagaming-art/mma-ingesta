"""One-off: stamp `trained_at` into the EXISTING model bundle without retraining.

Loads model.joblib, adds bundle['trained_at'] derived from the file's current
mtime (the real training time), and re-dumps. The model/imputer/calibrator
objects are preserved verbatim, so predictions stay byte-for-byte identical.
Idempotent: re-running is a no-op once the key exists.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import joblib

MODEL_PATH = Path(__file__).resolve().parents[1] / "src" / "prediction" / "model.joblib"


def main() -> None:
    bundle = joblib.load(MODEL_PATH)
    if not isinstance(bundle, dict):
        raise SystemExit("Unexpected bundle format (expected dict)")
    if bundle.get("trained_at"):
        print("Already stamped:", bundle["trained_at"])
        return
    mtime = os.path.getmtime(MODEL_PATH)
    trained_at = datetime.fromtimestamp(mtime, tz=timezone.utc).date().isoformat()
    bundle["trained_at"] = trained_at  # e.g. "2026-06-26"
    joblib.dump(bundle, MODEL_PATH)
    print("Stamped trained_at =", trained_at)


if __name__ == "__main__":
    main()
