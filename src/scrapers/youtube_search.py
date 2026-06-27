"""Minimal YouTube Data API v3 search client (full-fight curation, #43).

Deliberately tiny and dependency-light: it uses ``requests`` (already a project
dep) instead of google-api-python-client. The HTTP layer is injectable via the
``fetcher`` argument so tests never touch the network (the search needs a quota
key anyway, and the channel RSS feed cannot search by query)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import requests

LOGGER = logging.getLogger(__name__)

# Official UFC channel (verified, mirrors mma-app/src/lib/youtube.ts).
UFC_CHANNEL_ID = "UCvgfXK4nTYKudb0rFR6noLA"
SEARCH_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"

# A fetcher receives the request params and returns the parsed JSON body.
Fetcher = Callable[[dict], dict]


@dataclass(frozen=True)
class YouTubeVideo:
    video_id: str
    title: str
    channel_id: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


def _requests_fetcher(timeout: int = 15) -> Fetcher:
    def fetch(params: dict) -> dict:
        response = requests.get(SEARCH_ENDPOINT, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

    return fetch


def search_videos(
    query: str,
    api_key: str,
    *,
    channel_id: str = UFC_CHANNEL_ID,
    max_results: int = 3,
    fetcher: Fetcher | None = None,
) -> list[YouTubeVideo]:
    """Return up to ``max_results`` videos for ``query`` within a single channel."""
    fetch = fetcher or _requests_fetcher()
    params = {
        "part": "snippet",
        "type": "video",
        "channelId": channel_id,
        "q": query,
        "maxResults": max_results,
        "key": api_key,
    }
    data = fetch(params)
    videos: list[YouTubeVideo] = []
    for item in data.get("items", []):
        video_id = (item.get("id") or {}).get("videoId")
        if not video_id:
            continue
        snippet = item.get("snippet") or {}
        videos.append(
            YouTubeVideo(
                video_id=video_id,
                title=snippet.get("title", ""),
                channel_id=snippet.get("channelId", ""),
            )
        )
    return videos
