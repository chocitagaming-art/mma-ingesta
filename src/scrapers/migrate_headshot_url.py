from __future__ import annotations

from .config import get_settings
from .db import connect


def main() -> None:
    settings = get_settings()
    with connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE fighters ADD COLUMN IF NOT EXISTS headshot_url TEXT;")
        connection.commit()


if __name__ == "__main__":
    main()