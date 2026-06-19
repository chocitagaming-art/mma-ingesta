"""Translate pre-ESPN (English) news to Spanish and backfill their images.

The DB still holds English-source news (MMA Fighting, Sherdog, Cageside Press,
LowKick, MMA Junkie) scraped before the switch to ESPN Deportes. Instead of
deleting them, translate headline + summary to Spanish via Claude and fill
``image_url`` from each article's ``og:image``.

Targets every row whose source is not 'ESPN Deportes'. Translating already-
Spanish text is a no-op at temperature 0, so re-running is safe (it just re-spends
a few tokens). The source label is left untouched so the badge stays clean.

Run (writes to the DB):
    python -m src.scrapers.translate_news            # translate everything pending
    python -m src.scrapers.translate_news --dry-run  # show translations, no writes
    python -m src.scrapers.translate_news --limit 3  # only the first 3 rows
"""
from __future__ import annotations

import argparse
import json
import logging

from anthropic import Anthropic

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .news import fetch_og_image

LOGGER = logging.getLogger(__name__)

TRANSLATE_MODEL = "claude-sonnet-4-6"


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        # Strip a ```json ... ``` fence if the model added one.
        text = text.strip("`")
        brace = text.find("{")
        if brace != -1:
            text = text[brace:]
    return json.loads(text)


def _translate(client: Anthropic, headline: str, summary: str | None) -> tuple[str, str | None]:
    prompt = (
        "Traduce al español de España el siguiente titular y resumen de una noticia de MMA/UFC. "
        "Conserva los nombres propios de peleadores, eventos y organizaciones (UFC, PFL, etc.). "
        "Suena natural y periodístico, no literal. Devuelve SOLO un objeto JSON con las claves "
        '"headline" (string) y "summary" (string o null si el resumen está vacío).\n\n'
        f"Titular: {headline}\n"
        f"Resumen: {summary or ''}"
    )
    response = client.messages.create(
        model=TRANSLATE_MODEL,
        max_tokens=1000,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    payload = _extract_json(text)
    new_headline = str(payload.get("headline") or headline).strip()
    raw_summary = payload.get("summary")
    new_summary = str(raw_summary).strip() if raw_summary else summary
    return new_headline, new_summary


def translate_news(*, dry_run: bool = False, limit: int | None = None) -> dict[str, int]:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required to translate news.")
    client = Anthropic(api_key=settings.anthropic_api_key)

    counts = {"candidates": 0, "translated": 0, "image_filled": 0, "errors": 0}

    with connect(settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, headline, summary, url, image_url FROM news "
                "WHERE source IS DISTINCT FROM 'ESPN Deportes' "
                "ORDER BY published_at DESC NULLS LAST, id DESC"
            )
            rows = cursor.fetchall()
        connection.commit()

        if limit is not None:
            rows = rows[:limit]
        counts["candidates"] = len(rows)
        LOGGER.info("%d articles to translate", len(rows))

        for news_id, headline, summary, url, image_url in rows:
            try:
                new_headline, new_summary = _translate(client, headline, summary)
            except Exception as exc:  # noqa: BLE001 - keep going on a single failure
                LOGGER.warning("Translation failed for id=%s: %s", news_id, exc)
                counts["errors"] += 1
                continue

            new_image = image_url or (fetch_og_image(url) if url else None)

            if dry_run:
                LOGGER.info(
                    "[dry-run] %r -> %r | image=%s", headline[:50], new_headline[:50], bool(new_image)
                )
            else:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE news SET headline=%s, summary=%s, "
                        "image_url=COALESCE(%s, image_url) WHERE id=%s",
                        (new_headline, new_summary, new_image, news_id),
                    )
                connection.commit()

            counts["translated"] += 1
            if new_image and not image_url:
                counts["image_filled"] += 1

    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Translate English news to Spanish and backfill images.")
    parser.add_argument("--dry-run", action="store_true", help="Show translations; do not write to the DB.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many rows.")
    args = parser.parse_args()

    counts = translate_news(dry_run=args.dry_run, limit=args.limit)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
