"""Match each upcoming event to its official UFC "Fighter Face-offs" (careo)
video and store the YouTube id (migration 022).

UFC publishes the face-off the evening before an event as
"UFC <City>: Fighter Face-offs" (Fight Nights) or "UFC <N>: ... Face-offs"
(numbered PPVs) on its official channel. We read the channel RSS feed (no API
key needed) and match a video to an event; the event page embeds it next to the
poster.

MATCH GUARD (conservative, mirrors backfill_fight_videos.is_trusted_match) — a
video is accepted only when ALL hold on the accent-stripped, casefolded title:
  1. matches face-?offs?  (whitelist; excludes "Ceremonial Weigh-In", "Weigh-Ins")
  2. contains the event CITY (from events.location) OR the "ufc <N>" token (name)
  3. published within [event_date - 5d, event_date + 1d]
Better to miss than to mis-attribute: an unmatched event stays NULL and retries
on the next daily run. Writes are first-writer-wins (set_event_faceoff_video).

Usage:
    python -m src.scrapers.match_faceoffs --dump-feed   # print the channel feed, no DB
    python -m src.scrapers.match_faceoffs               # dry-run: report matches, no writes
    python -m src.scrapers.match_faceoffs --apply       # persist matches
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

import requests

from .config import get_settings
from .db import connect
from .logging_config import configure_logging
from .matching import strip_accents
from .repositories.events import set_event_faceoff_video

LOGGER = logging.getLogger(__name__)

# Official UFC channel. RSS gives the latest ~15 uploads (id + title + date) with
# no API key. The face-off publishes the day before, so it's always in-window.
UFC_CHANNEL_ID = "UCvgfXK4nTYKudb0rFR6noLA"
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; mma-ingesta/1.0)"}

_FACEOFF_RE = re.compile(r"face-?offs?\b", re.IGNORECASE)
_UFC_NUM_RE = re.compile(r"\bufc\s*(\d{2,4})\b", re.IGNORECASE)
# Careos publish the evening before; allow a small window around the event date.
_DAYS_BEFORE = 5
_DAYS_AFTER = 1


@dataclass(frozen=True)
class FeedVideo:
    video_id: str
    title: str
    published: date


@dataclass(frozen=True)
class TargetEvent:
    id: int
    name: str
    location: str | None
    event_date: date | None


def parse_feed(xml_text: str) -> list[FeedVideo]:
    """Parse a YouTube channel Atom feed into (videoId, title, published date)."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        LOGGER.warning("Feed parse error: %s", exc)
        return []
    videos: list[FeedVideo] = []
    for entry in root.findall(f"{_ATOM}entry"):
        vid = entry.findtext(f"{_YT}videoId")
        title = entry.findtext(f"{_ATOM}title")
        published = entry.findtext(f"{_ATOM}published")
        if not vid or not title or not published:
            continue
        try:
            pub_date = datetime.fromisoformat(published).date()
        except ValueError:
            continue
        videos.append(FeedVideo(video_id=vid.strip(), title=title.strip(), published=pub_date))
    return videos


def fetch_channel_feed(
    session: requests.Session, channel_id: str = UFC_CHANNEL_ID
) -> list[FeedVideo]:
    """Latest uploads of a YouTube channel via its public RSS (no API key)."""
    try:
        response = session.get(RSS_URL.format(channel_id=channel_id), headers=_HEADERS, timeout=20)
    except requests.RequestException as exc:
        LOGGER.warning("Feed fetch failed: %s", exc)
        return []
    if not response.ok:
        LOGGER.warning("Feed HTTP %s", response.status_code)
        return []
    return parse_feed(response.text)


def _norm(text: str | None) -> str:
    return strip_accents(text or "").casefold()


def event_city(location: str | None) -> str | None:
    """City from 'Venue, City, State, Country' (2nd comma field), else the 1st.
    'Paycom Center, Oklahoma City, OK, United States' -> 'Oklahoma City'."""
    if not location:
        return None
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts else None


def match_event(event: TargetEvent, videos: list[FeedVideo]) -> str | None:
    """First feed video that satisfies the conservative guard, else None."""
    if event.event_date is None:
        return None
    city_n = _norm(event_city(event.location))
    num_match = _UFC_NUM_RE.search(event.name or "")
    ufc_num = num_match.group(1) if num_match else None
    lo = event.event_date - timedelta(days=_DAYS_BEFORE)
    hi = event.event_date + timedelta(days=_DAYS_AFTER)
    for video in videos:
        if not (lo <= video.published <= hi):
            continue
        title_n = _norm(video.title)
        if not _FACEOFF_RE.search(title_n):
            continue
        city_ok = bool(city_n and city_n in title_n)
        num_ok = bool(ufc_num and re.search(rf"\bufc\s*{ufc_num}\b", title_n))
        if city_ok or num_ok:
            return video.video_id
    return None


def get_target_events(connection) -> list[TargetEvent]:
    """Upcoming events (from ~2 days ago onward) that still lack a face-off."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name, location, event_date
            FROM events
            WHERE status = 'upcoming'
              AND event_date IS NOT NULL
              AND event_date >= CURRENT_DATE - INTERVAL '2 days'
              AND faceoff_video_id IS NULL
            ORDER BY event_date ASC
            """
        )
        return [
            TargetEvent(int(r[0]), str(r[1]), r[2], r[3]) for r in cursor.fetchall()
        ]


def run(connection, *, apply: bool, feed: list[FeedVideo]) -> Counter:
    counts: Counter = Counter()
    events = get_target_events(connection)
    counts["targets"] = len(events)
    for event in events:
        video_id = match_event(event, feed)
        if not video_id:
            counts["no_match"] += 1
            continue
        counts["matched"] += 1
        LOGGER.info("Event %d %r -> face-off %s", event.id, event.name, video_id)
        if apply and set_event_faceoff_video(connection, event.id, video_id):
            connection.commit()
            counts["written"] += 1
    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Match UFC face-off (careo) videos to upcoming events."
    )
    parser.add_argument("--apply", action="store_true", help="Persist matches (default: dry-run).")
    parser.add_argument("--dump-feed", action="store_true", help="Print the channel RSS feed and exit (no DB).")
    args = parser.parse_args()

    session = requests.Session()
    feed = fetch_channel_feed(session)
    if args.dump_feed:
        for video in feed:
            print(f"{video.video_id} | {video.published} | {video.title}")
        return
    if not feed:
        raise SystemExit("Empty channel feed — aborting (no writes).")

    settings = get_settings()
    with connect(settings.database_url) as connection:
        counts = run(connection, apply=args.apply, feed=feed)

    print(json.dumps({k: counts.get(k, 0) for k in ["targets", "matched", "no_match", "written"]}, indent=2))
    if not args.apply:
        print("Dry-run: nothing written. Re-run with --apply to persist.")


if __name__ == "__main__":
    main()
