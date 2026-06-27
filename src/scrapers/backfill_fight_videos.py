"""Curate official UFC full-fight video URLs onto completed fights (#43).

For each completed bout that still has no ``video_url`` we search the official UFC
YouTube channel for "<red> vs <blue>" and, ONLY when a confidence guard passes
(official channel AND the title contains both fighters' surnames), propose that
video. Dry-run by default (prints a table, writes nothing); --apply persists the
proposals with an idempotent UPDATE.

SAFETY (Phase-1 pattern): without --apply NOTHING is written. The YouTube search
requires a quota key (the channel RSS feed cannot search by query), so a missing
YOUTUBE_API_KEY exits early without touching the DB.

Usage:
    # preview main events, no writes:
    python -m src.scrapers.backfill_fight_videos --limit 20

    # also include the rest of the main card (bout_order <= 5):
    python -m src.scrapers.backfill_fight_videos --max-bout-order 5 --event "UFC 300"

    # persist the proposed URLs:
    python -m src.scrapers.backfill_fight_videos --limit 20 --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from dotenv import load_dotenv

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .youtube_search import UFC_CHANNEL_ID, YouTubeVideo, search_videos

LOGGER = logging.getLogger(__name__)

# search_client(query, api_key) -> candidate videos. Injected in tests.
SearchClient = Callable[[str, str], list[YouTubeVideo]]

CONFIDENCE = "ufc_official+fight_video+both_surnames"

# The title must look like an actual fight video, not press/promo content. UFC's
# official channel titles full bouts as "Free Fight: …" and recaps as "… Fight
# Highlights"; everything else (pressers, weigh-ins, embedded, promos) is noise.
_POSITIVE_KEYWORDS = ("free fight", "full fight", "fight highlights", "highlights")
_NEGATIVE_KEYWORDS = (
    "press conference", "presser", "weigh-in", "weigh in", "weigh-ins",
    "embedded", "promo", "preview", "trailer", "ceremonial", "face-off",
    "faceoff", "media day", "top 5", "top 10", "best of", "interview",
    "recap", "countdown", "open workout", "staredown",
)


@dataclass(frozen=True)
class FightRow:
    fight_id: int
    bout_order: int | None
    red_name: str
    blue_name: str
    event_name: str
    event_date: object


@dataclass(frozen=True)
class Proposal:
    fight_id: int
    red_name: str
    blue_name: str
    url: str
    title: str
    confidence: str


def _surname(name: str) -> str:
    parts = name.strip().split()
    return parts[-1] if parts else ""


def _normalize(text: str) -> str:
    """Lowercase and strip accents so 'Quiñónez' matches a plain-ASCII title."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.lower()


def _contains_word(haystack: str, word: str) -> bool:
    """Whole-word match so a short surname ('Lee') doesn't hit 'asleep'."""
    return bool(word) and re.search(rf"\b{re.escape(word)}\b", haystack) is not None


def _looks_like_fight_video(normalized_title: str) -> bool:
    """A real fight video: has a positive marker and no press/promo marker."""
    if any(bad in normalized_title for bad in _NEGATIVE_KEYWORDS):
        return False
    return any(good in normalized_title for good in _POSITIVE_KEYWORDS)


def is_trusted_match(
    video: YouTubeVideo,
    red_name: str,
    blue_name: str,
    channel_id: str = UFC_CHANNEL_ID,
) -> bool:
    """Accept a candidate ONLY when ALL hold: it is the official UFC channel, the
    title looks like a fight video (not a presser/weigh-in/promo), and it names
    both fighters' surnames as whole words (accent-insensitive). Conservative on
    purpose — better to skip and leave for manual curation than mis-curate."""
    if video.channel_id != channel_id:
        return False
    red_surname = _normalize(_surname(red_name))
    blue_surname = _normalize(_surname(blue_name))
    if not red_surname or not blue_surname:
        return False
    title = _normalize(video.title)
    if not _looks_like_fight_video(title):
        return False
    return _contains_word(title, red_surname) and _contains_word(title, blue_surname)


def select_fights(
    connection,
    *,
    max_bout_order: int,
    limit: int | None,
    event: str | None = None,
) -> list[FightRow]:
    """Completed bouts (winner or method known) with no video_url and bout_order <= N."""
    clauses = [
        "f.video_url IS NULL",
        "(f.winner_id IS NOT NULL OR f.method IS NOT NULL)",
        "f.bout_order IS NOT NULL",
        "f.bout_order <= %s",
    ]
    params: list = [max_bout_order]
    if event:
        clauses.append("e.name ILIKE %s")
        params.append(f"%{event}%")
    sql = (
        "SELECT f.id, f.bout_order, "
        "COALESCE(fr.name, f.fighter_red_name) AS red_name, "
        "COALESCE(fb.name, f.fighter_blue_name) AS blue_name, "
        "e.name AS event_name, e.event_date "
        "FROM fights f "
        "LEFT JOIN fighters fr ON fr.id = f.fighter_red_id "
        "LEFT JOIN fighters fb ON fb.id = f.fighter_blue_id "
        "LEFT JOIN events e ON e.id = f.event_id "
        "WHERE " + " AND ".join(clauses) +
        " ORDER BY e.event_date DESC NULLS LAST, f.bout_order ASC"
    )
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
    return [
        FightRow(
            fight_id=int(row[0]),
            bout_order=int(row[1]) if row[1] is not None else None,
            red_name=row[2] or "",
            blue_name=row[3] or "",
            event_name=row[4] or "",
            event_date=row[5],
        )
        for row in rows
    ]


def _update_video_url(connection, fight_id: int, video_url: str) -> int:
    """Idempotent write: only fills a still-empty video_url. Returns rows affected."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE fights SET video_url = %s, updated_at = NOW() "
            "WHERE id = %s AND video_url IS NULL",
            (video_url, fight_id),
        )
        return cursor.rowcount


def backfill(
    connection,
    *,
    api_key: str,
    max_bout_order: int = 1,
    limit: int | None = None,
    event: str | None = None,
    apply: bool = False,
    search_client: SearchClient = search_videos,
) -> tuple[list[Proposal], Counter]:
    counts: Counter = Counter()
    proposals: list[Proposal] = []
    fights = select_fights(
        connection, max_bout_order=max_bout_order, limit=limit, event=event
    )
    counts["candidates"] = len(fights)
    for fight in fights:
        query = f"{fight.red_name} vs {fight.blue_name}"
        try:
            videos = search_client(query, api_key)
        except Exception as exc:  # noqa: BLE001 - isolate per-fight network failures
            counts["errors"] += 1
            LOGGER.warning("YouTube search failed for fight %s (%s): %s", fight.fight_id, query, exc)
            continue
        match = next(
            (v for v in videos if is_trusted_match(v, fight.red_name, fight.blue_name)),
            None,
        )
        if match is None:
            counts["no_candidate"] += 1
            LOGGER.info("No trusted candidate for fight %s: %s", fight.fight_id, query)
            continue
        proposals.append(
            Proposal(
                fight_id=fight.fight_id,
                red_name=fight.red_name,
                blue_name=fight.blue_name,
                url=match.url,
                title=match.title,
                confidence=CONFIDENCE,
            )
        )
        counts["proposed"] += 1
        if apply:
            # Isolate each write (mirrors backfill_fight_stats): a transient DB
            # failure rolls back just this fight and the batch keeps going.
            try:
                written = _update_video_url(connection, fight.fight_id, match.url)
                if written:
                    counts["written"] += 1
                    connection.commit()
                else:
                    counts["already_set"] += 1
                    connection.rollback()
            except Exception as exc:  # noqa: BLE001 - isolate per-fight DB failures
                counts["errors"] += 1
                connection.rollback()
                LOGGER.warning("Write failed for fight %s: %s", fight.fight_id, exc)
                continue
    if not apply:
        # Release the read-only snapshot; guarantees dry-run never commits.
        connection.rollback()
    return proposals, counts


def _print_table(proposals: list[Proposal]) -> None:
    if not proposals:
        print("No trusted video candidates found.")
        return
    print(f"{'fight_id':>8}  {'red':<22}  {'blue':<22}  {'confidence':<26}  url | title")
    for p in proposals:
        print(
            f"{p.fight_id:>8}  {p.red_name[:22]:<22}  {p.blue_name[:22]:<22}  "
            f"{p.confidence:<26}  {p.url} | {p.title}"
        )


def main() -> None:
    configure_logging()
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Curate official UFC full-fight video URLs (#43). Dry-run by default."
    )
    parser.add_argument("--apply", action="store_true", help="Write video_url to the DB. Default: dry-run (no writes).")
    parser.add_argument("--limit", type=int, default=25,
                        help="Max fights to process. Each one costs ~100 YouTube quota units "
                             "(daily cap 10k), so default is 25; pass 0 for no limit.")
    parser.add_argument("--max-bout-order", type=int, default=1, dest="max_bout_order",
                        help="Only fights with bout_order <= N (1 = main event only, 5 = whole main card).")
    parser.add_argument("--event", default=None, help="Only events whose name contains this substring (ILIKE).")
    args = parser.parse_args()
    # --limit 0 means "no limit".
    limit = None if args.limit == 0 else args.limit

    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        print(
            "YOUTUBE_API_KEY is not set. The YouTube search requires a quota key "
            "(the channel RSS feed cannot search by query). No DB changes made."
        )
        sys.exit(1)

    settings = get_settings()
    with connect(settings.database_url) as connection:
        proposals, counts = backfill(
            connection,
            api_key=api_key,
            max_bout_order=args.max_bout_order,
            limit=limit,
            event=args.event,
            apply=args.apply,
        )

    _print_table(proposals)
    mode = "APPLY" if args.apply else "DRY-RUN (no writes)"
    print(
        f"\n[{mode}] candidates={counts['candidates']} proposed={counts['proposed']} "
        f"written={counts['written']} already_set={counts['already_set']} "
        f"no_candidate={counts['no_candidate']} errors={counts['errors']}"
    )
    if not args.apply:
        print("Dry-run: nothing was written. Re-run with --apply to persist.")


if __name__ == "__main__":
    main()
