from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg2.extensions import connection as PgConnection


@dataclass(frozen=True)
class NewsArticleRecord:
    headline: str
    summary: str | None
    source: str
    url: str
    published_at: datetime | None
    fighter_id: int | None
    category: str
    relevance: int
    image_url: str | None = None


def get_existing_news_urls(connection: PgConnection) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT url FROM news WHERE url IS NOT NULL")
        return {row[0] for row in cursor.fetchall() if row[0]}


def upsert_news_article(connection: PgConnection, article: NewsArticleRecord) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO news (
                headline, summary, source, url, published_at, fighter_id, category, relevance, image_url
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (url)
            DO UPDATE SET
                headline = EXCLUDED.headline,
                summary = EXCLUDED.summary,
                source = EXCLUDED.source,
                published_at = EXCLUDED.published_at,
                fighter_id = EXCLUDED.fighter_id,
                category = EXCLUDED.category,
                relevance = EXCLUDED.relevance,
                image_url = COALESCE(EXCLUDED.image_url, news.image_url)
            RETURNING id
            """,
            (
                article.headline,
                article.summary,
                article.source,
                article.url,
                article.published_at,
                article.fighter_id,
                article.category,
                article.relevance,
                article.image_url,
            ),
        )
        return int(cursor.fetchone()[0])