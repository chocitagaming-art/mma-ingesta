"""Smoke: the UFC YouTube channel RSS feed still parses into videos.

Protects refresh-faceoffs (match_faceoffs.py). Only feed STRUCTURE is
asserted — a face-off video is NOT required to be present (they publish the
evening before an event, so most days the window has none).
"""

from __future__ import annotations

import pytest

from src.scrapers.match_faceoffs import RSS_URL, UFC_CHANNEL_ID, parse_feed


pytestmark = pytest.mark.smoke

# The Atom feed always carries the channel's ~15 latest uploads (15 observed
# 2026-07); the very active UFC channel can never sit below 5.
MIN_FEED_VIDEOS = 5


def test_channel_feed_parses_videos(rss_session, retry_fetch):
    url = RSS_URL.format(channel_id=UFC_CHANNEL_ID)

    def download() -> str:
        # production fetch_channel_feed swallows errors and returns []; here the
        # download is separate so HTTP failures are never read as a parse break.
        response = rss_session.get(url, timeout=20)
        response.raise_for_status()
        return response.text

    xml_text = retry_fetch(f"GET {url}", download)
    videos = parse_feed(xml_text)
    assert len(videos) >= MIN_FEED_VIDEOS, (
        f"PARSER BREAK: the channel feed arrived ({len(xml_text)} chars of XML) but "
        f"parse_feed extracted only {len(videos)} videos (threshold {MIN_FEED_VIDEOS}) "
        f"— the Atom namespaces/entry shape likely changed"
    )
    incomplete = [
        v for v in videos if not (v.video_id and v.title and v.published)
    ]
    assert not incomplete, (
        f"PARSER BREAK: {len(incomplete)} of {len(videos)} parsed videos are missing "
        f"video_id/title/published — parse_feed's field extraction drifted"
    )
