"""One-off: add news.image_url and backfill it for existing articles.

The regular news scraper skips URLs already in the DB, so existing articles
would never get an image. This re-fetches the RSS feeds (cheap, no Claude) and
fills `image_url` for rows whose URL still appears in the current feeds.

Run: python -m src.scrapers.backfill_news_images
"""

from __future__ import annotations

import json
import logging

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .news import fetch_feed_articles

LOGGER = logging.getLogger(__name__)


def backfill(max_articles: int = 400) -> dict[str, int]:
    settings = get_settings()
    articles = fetch_feed_articles(max_articles=max_articles)
    with_image = [a for a in articles if a.image_url]
    rows_updated = 0
    with connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS image_url TEXT")
            for article in with_image:
                cursor.execute(
                    """
                    UPDATE news
                    SET image_url = %s
                    WHERE url = %s AND (image_url IS NULL OR image_url = '')
                    """,
                    (article.image_url, article.url),
                )
                rows_updated += cursor.rowcount
        connection.commit()
    return {
        "fetched": len(articles),
        "feed_articles_with_image": len(with_image),
        "rows_updated": rows_updated,
    }


def main() -> None:
    configure_logging()
    print(json.dumps(backfill(), indent=2))


if __name__ == "__main__":
    main()
