"""Match each upcoming event to its official UFC "Fighter Face-offs" (careo)
video and store the YouTube id (migration 022).

UFC publishes the face-off the evening before an event as
"UFC <City>: Fighter Face-offs" (Fight Nights) or "UFC <N>: ... Face-offs"
(numbered PPVs) on its official channel. We read the channel RSS feed (no API
key needed) and match a video to an event; the event page embeds it next to the
poster.

RESCUE PASS (opt-in): the RSS feed only carries the ~15 latest uploads, so a
careo drops out of it once an event passes and newer videos pile on. When a
YOUTUBE_API_KEY is present, a second pass pages the channel's uploads playlist
(playlistItems, 1 quota unit/page) back over the last ``--rescue-days`` and
rescues face-offs for recently-passed events the RSS could no longer reach.
Same conservative guard and first-writer-wins write; without a key the cron
runs RSS-only, exactly as before.

MATCH GUARD (conservative, mirrors backfill_fight_videos.is_trusted_match) — a
video is accepted only when ALL hold on the accent-stripped, casefolded title:
  1. matches face-?offs?  (whitelist; excludes "Ceremonial Weigh-In", "Weigh-Ins")
  2. contains a distinctive place token of the event — a city token, or the
     'vegas' alias for Nevada/Apex cards — OR the "ufc <N>" card number (name)
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
import os
import re
from collections import Counter
from collections.abc import Callable
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

# --- Optional YouTube Data API rescue (opt-in via YOUTUBE_API_KEY) --------
# The RSS feed only exposes the ~15 latest uploads, so a careo drops out of it
# once newer videos pile on (e.g. an event that just passed). playlistItems on
# the channel's uploads playlist pages further back at 1 quota unit/page (vs 100
# for search.list), letting the daily cron RESCUE those missed face-offs.
PLAYLIST_ITEMS_ENDPOINT = "https://www.googleapis.com/youtube/v3/playlistItems"
_RESCUE_LOOKBACK_DAYS = 45
# Safety cap on pagination. The published_after early-stop normally trips first;
# 20 pages (~1000 videos, 20 quota units of 10k/day) is enough to reach ~45 days
# back even on the very active UFC channel.
_UPLOADS_MAX_PAGES = 20

# A fetcher receives the request params and returns the parsed JSON body
# (injected in tests so the rescue path never touches the network).
_PlaylistFetcher = Callable[[dict], dict]


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


def uploads_playlist_id(channel_id: str = UFC_CHANNEL_ID) -> str:
    """A channel's uploads playlist id: the channel id with its 'UC' prefix
    swapped for 'UU' (a stable YouTube convention). Lets us page every upload via
    playlistItems (1 quota unit/page) without an extra channels.list call."""
    return "UU" + channel_id[2:] if channel_id.startswith("UC") else channel_id


def parse_playlist_page(data: dict) -> list[FeedVideo]:
    """Parse one playlistItems API page into FeedVideo (id, title, published).

    Prefers contentDetails.videoPublishedAt (the real upload time) and falls back
    to the snippet fields; skips items missing any of id/title/date so a partial
    entry never becomes a bogus match."""
    videos: list[FeedVideo] = []
    for item in data.get("items", []):
        content = item.get("contentDetails") or {}
        snippet = item.get("snippet") or {}
        vid = content.get("videoId")
        if not vid:
            vid = (snippet.get("resourceId") or {}).get("videoId")
        title = snippet.get("title")
        raw = content.get("videoPublishedAt") or snippet.get("publishedAt")
        if not vid or not title or not raw:
            continue
        try:
            pub = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        videos.append(FeedVideo(vid.strip(), title.strip(), pub))
    return videos


def _requests_playlist_fetcher(timeout: int = 15) -> _PlaylistFetcher:
    def fetch(params: dict) -> dict:
        response = requests.get(PLAYLIST_ITEMS_ENDPOINT, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

    return fetch


def fetch_channel_uploads(
    api_key: str,
    *,
    published_after: date | None = None,
    channel_id: str = UFC_CHANNEL_ID,
    max_pages: int = _UPLOADS_MAX_PAGES,
    fetcher: _PlaylistFetcher | None = None,
) -> list[FeedVideo]:
    """Recent uploads of a channel via playlistItems (needs a quota key), newest
    first, paging back until an item predates ``published_after`` or ``max_pages``
    is hit. Returns FeedVideo objects so match_event reuses the exact RSS guard."""
    fetch = fetcher or _requests_playlist_fetcher()
    playlist_id = uploads_playlist_id(channel_id)
    videos: list[FeedVideo] = []
    page_token: str | None = None
    for _ in range(max_pages):
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        data = fetch(params)
        reached_old = False
        for video in parse_playlist_page(data):
            if published_after is not None and video.published < published_after:
                reached_old = True
                continue
            videos.append(video)
        page_token = data.get("nextPageToken")
        if reached_old or not page_token:
            break
    return videos


def _norm(text: str | None) -> str:
    return strip_accents(text or "").casefold()


# Words that do NOT tell one card apart from another, so they cannot act as a
# guard. Three groups, all chosen from the 185 real `events.location` rows:
#  - generic articles/fillers that survive the 3-char cut,
#  - VENUE TYPES: they name a building, not a place. "Belgrade Arena" is
#    Belgrade; "arena" on its own would match any other card's faceoff video,
#  - country/state words too common to discriminate: `usa` appears in 91 of the
#    185 rows, so it would green-light almost any US event,
#  - SPORT words that live inside venue names. This one bit for real: reading
#    the whole `location` pulled `ufc` out of "UFC Apex, Las Vegas", and `ufc`
#    is in EVERY faceoff title, so a Vegas card matched Oklahoma City's video.
#    `test_match_vegas_event_rejects_other_citys_faceoff` caught it.
_PLACE_STOPWORDS = frozenset(
    {
        "ufc", "mma", "fight", "fights", "night", "octagon",
        "city", "the", "las", "los", "san", "de", "el", "st", "new",
        "arena", "center", "centre", "stadium", "coliseum", "hall", "garden",
        "gardens", "forum", "dome", "pavilion", "park", "place", "complex",
        "casino", "resort", "hotel", "theater", "theatre", "sports", "field",
        "palace", "expo", "convention", "entertainment", "grounds",
        "usa", "united", "states", "kingdom", "america", "american",
    }
)


def _place_tokens(location: str | None) -> set[str]:
    """Distinctive, whole-word place tokens for the city guard.

    READS THE WHOLE `location`, NOT ONE COMMA FIELD — and that IS the fix
    (2026-08-02). The old version took the 2nd field as "the city", which the
    real data contradicts twice over:

      'Belgrade Arena, BG, Serbia'      -> 2nd field 'BG', 2 chars, dropped by
                                           the length cut -> EMPTY SET, and
                                           `any()` over an empty set is False
                                           ALWAYS. The 1063 faceoff was lost
                                           this way and had to be matched by
                                           hand; 'Washington, DC, USA' is the
                                           same shape.
      'London, England, United Kingdom' -> 2nd field is the STATE/REGION, so
                                           the token was 'england' and a video
                                           titled "UFC London ... Faceoffs"
                                           never matched. Same for
                                           'Miami, Florida, USA' and 8 more.

    The 31-jul patch proposed raising the length cut to 4 instead. That attacks
    the wrong thing and has a counterexample: 'Rio de Janeiro' would lose 'rio'.
    The cut STAYS at 3; what changes is where the words come from.

    UFC titles Las Vegas / Apex Fight Nights "UFC Vegas NNN: Fighter Faceoffs"
    (not "Las Vegas"), and some rows carry 'Nevada' as the city, so any card in
    Nevada / the Apex also accepts the 'vegas' token.
    """
    palabras = (t.strip(".") for t in _norm(location).replace(",", " ").split())
    tokens = {t for t in palabras if len(t) >= 3 and t not in _PLACE_STOPWORDS}
    full = _norm(location)
    if "vegas" in full or "nevada" in full or "apex" in full:
        tokens.add("vegas")
    return tokens


def _title_has_place_token(title_n: str, tokens: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(tok)}\b", title_n) for tok in tokens)


def match_event(event: TargetEvent, videos: list[FeedVideo]) -> str | None:
    """First feed video that satisfies the conservative guard, else None."""
    if event.event_date is None:
        return None
    place_tokens = _place_tokens(event.location)
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
        city_ok = _title_has_place_token(title_n, place_tokens)
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


def get_rescue_target_events(
    connection, lookback_days: int = _RESCUE_LOOKBACK_DAYS
) -> list[TargetEvent]:
    """Recently-passed / imminent events still missing a face-off, for the API
    rescue pass. Unlike get_target_events this does NOT require status='upcoming'
    (a passed event's careo is exactly what the RSS window can no longer reach),
    but stays bounded to the last ``lookback_days`` so we never chase careos for
    old cards that never had one."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, name, location, event_date
            FROM events
            WHERE event_date IS NOT NULL
              AND event_date >= CURRENT_DATE - make_interval(days => %s)
              AND event_date <= CURRENT_DATE + INTERVAL '1 day'
              AND faceoff_video_id IS NULL
            ORDER BY event_date DESC
            """,
            (lookback_days,),
        )
        return [
            TargetEvent(int(r[0]), str(r[1]), r[2], r[3]) for r in cursor.fetchall()
        ]


def run(
    connection,
    *,
    apply: bool,
    feed: list[FeedVideo],
    api_feed: list[FeedVideo] | None = None,
    rescue_days: int = _RESCUE_LOOKBACK_DAYS,
) -> Counter:
    counts: Counter = Counter()
    matched_ids: set[int] = set()

    # Pass 1 — RSS (free, no quota): upcoming events within the ~15-video window.
    events = get_target_events(connection)
    counts["targets"] = len(events)
    for event in events:
        video_id = match_event(event, feed)
        if not video_id:
            counts["no_match"] += 1
            continue
        counts["matched"] += 1
        matched_ids.add(event.id)
        LOGGER.info("Event %d %r -> face-off %s (rss)", event.id, event.name, video_id)
        if apply and set_event_faceoff_video(connection, event.id, video_id):
            connection.commit()
            counts["written"] += 1

    # Pass 2 — YouTube Data API rescue (opt-in): recently-passed events whose
    # careo already dropped out of the RSS window. Same conservative guard.
    if api_feed is not None:
        rescue_events = get_rescue_target_events(connection, rescue_days)
        counts["rescue_targets"] = len(rescue_events)
        for event in rescue_events:
            if event.id in matched_ids:
                continue  # RSS already filled it this run
            video_id = match_event(event, api_feed)
            if not video_id:
                counts["no_match"] += 1
                continue
            counts["rescued"] += 1
            matched_ids.add(event.id)
            LOGGER.info(
                "Event %d %r -> face-off %s (api rescue)",
                event.id,
                event.name,
                video_id,
            )
            if apply and set_event_faceoff_video(connection, event.id, video_id):
                connection.commit()
                counts["written"] += 1
    return counts


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Match UFC face-off (careo) videos to events (RSS + API rescue)."
    )
    parser.add_argument("--apply", action="store_true", help="Persist matches (default: dry-run).")
    parser.add_argument("--dump-feed", action="store_true", help="Print the channel RSS feed and exit (no DB).")
    parser.add_argument(
        "--rescue-days", type=int, default=_RESCUE_LOOKBACK_DAYS, dest="rescue_days",
        help="API rescue look-back window in days (needs YOUTUBE_API_KEY). Default 45.",
    )
    args = parser.parse_args()

    session = requests.Session()
    feed = fetch_channel_feed(session)
    if args.dump_feed:
        for video in feed:
            print(f"{video.video_id} | {video.published} | {video.title}")
        return
    if not feed:
        raise SystemExit("Empty channel feed — aborting (no writes).")

    # Optional API rescue: only when a quota key is present. Without it the cron
    # behaves exactly as before (RSS only) — graceful, zero-cost degradation.
    api_feed: list[FeedVideo] | None = None
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if api_key:
        cutoff = date.today() - timedelta(days=args.rescue_days + _DAYS_BEFORE + 2)
        try:
            api_feed = fetch_channel_uploads(api_key, published_after=cutoff)
            LOGGER.info("API rescue: %d uploads since %s", len(api_feed), cutoff)
        except Exception as exc:  # noqa: BLE001 - never let rescue break the RSS run
            LOGGER.warning("API rescue fetch failed (%s); continuing RSS-only.", exc)
            api_feed = None
    else:
        LOGGER.info("No YOUTUBE_API_KEY: RSS-only (no API rescue).")

    settings = get_settings()
    with connect(settings.database_url) as connection:
        counts = run(
            connection, apply=args.apply, feed=feed,
            api_feed=api_feed, rescue_days=args.rescue_days,
        )

    keys = ["targets", "matched", "rescue_targets", "rescued", "no_match", "written"]
    print(json.dumps({k: counts.get(k, 0) for k in keys}, indent=2))
    if not args.apply:
        print("Dry-run: nothing written. Re-run with --apply to persist.")


if __name__ == "__main__":
    main()
