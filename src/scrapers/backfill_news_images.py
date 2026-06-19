"""One-off: add news.image_url and backfill it for existing articles.

The regular news scraper skips URLs already in the DB, so existing articles
would never get an image. This walks every row whose ``image_url`` is still
empty and fills it, preferring the image shipped in the current RSS feed and
falling back to the article page's ``og:image`` (needed for feeds like ESPN
Deportes that don't put an image in the feed itself).

``--dry-run`` does not modify any DATA (no image_url is written); it still
ensures the column exists so the scan can run.

Run (writes to the DB):
    python -m src.scrapers.backfill_news_images            # backfill everything
    python -m src.scrapers.backfill_news_images --dry-run  # report only, no data writes
    python -m src.scrapers.backfill_news_images --limit 5  # process at most 5 rows
    python -m src.scrapers.backfill_news_images --no-og    # feed images only
"""

from __future__ import annotations

import argparse
import json
import logging

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .news import fetch_feed_articles, fetch_og_image

LOGGER = logging.getLogger(__name__)


def _build_feed_image_map(max_articles: int = 400) -> dict[str, str]:
    """Map article URL -> image URL from the current RSS feeds that ship one."""
    feed_map: dict[str, str] = {}
    for article in fetch_feed_articles(max_articles=max_articles):
        if article.image_url:
            feed_map[article.url] = article.image_url
    return feed_map


def backfill(
    *,
    dry_run: bool = False,
    limit: int | None = None,
    use_og: bool = True,
    max_articles: int = 400,
) -> dict[str, int]:
    settings = get_settings()
    feed_map = _build_feed_image_map(max_articles=max_articles)
    LOGGER.info("Feed image map has %d entries", len(feed_map))

    counts = {
        "missing": 0,
        "from_feed": 0,
        "from_og": 0,
        "resolved": 0,
        "rows_updated": 0,
        "still_missing": 0,
    }

    with connect(settings.database_url) as connection:
        # Ensure the column exists in its OWN committed transaction, so the DDL
        # ACCESS EXCLUSIVE lock on `news` is released immediately instead of
        # being held across the slow per-article HTTP fetches below.
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS image_url TEXT")
        connection.commit()

        # Materialize the worklist and close the read transaction BEFORE the
        # network loop, so the connection isn't "idle in transaction" during the
        # og:image fetches (which would block autovacuum on a busy DB).
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, url FROM news "
                "WHERE (image_url IS NULL OR image_url = '') AND url IS NOT NULL "
                "ORDER BY published_at DESC NULLS LAST, id DESC"
            )
            rows = cursor.fetchall()
        connection.commit()

        if limit is not None:
            rows = rows[:limit]
        counts["missing"] = len(rows)
        LOGGER.info("%d articles are missing an image", len(rows))

        for news_id, url in rows:
            image_url = feed_map.get(url)
            if image_url:
                counts["from_feed"] += 1
            elif use_og:
                image_url = fetch_og_image(url)
                if image_url:
                    counts["from_og"] += 1

            if not image_url:
                counts["still_missing"] += 1
                LOGGER.debug("No image found for %s", url)
                continue

            counts["resolved"] += 1
            if dry_run:
                LOGGER.info("[dry-run] would set %s -> %s", url, image_url)
                continue

            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE news SET image_url = %s WHERE id = %s",
                    (image_url, news_id),
                )
            connection.commit()
            counts["rows_updated"] += 1

    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Backfill image_url for existing news rows."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only; do not write image_url to the DB.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Process at most this many rows."
    )
    parser.add_argument(
        "--no-og",
        action="store_true",
        help="Skip the og:image page-fetch fallback (use feed images only).",
    )
    args = parser.parse_args()

    counts = backfill(dry_run=args.dry_run, limit=args.limit, use_og=not args.no_og)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
