"""Make the repo root importable so tests can `from src.prediction... import`
the same way the app runs it (`python -m src...`)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
