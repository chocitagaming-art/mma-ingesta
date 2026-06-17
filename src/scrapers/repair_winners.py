from __future__ import annotations

from .logging_config import configure_logging
from .main import repair_fight_winners


def main() -> None:
    configure_logging()
    counts = repair_fight_winners()
    print(dict(counts))


if __name__ == "__main__":
    main()