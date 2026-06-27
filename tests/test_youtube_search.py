"""search_videos parses the YouTube Data API response into YouTubeVideo objects,
skips items without a videoId, tolerates a missing snippet, and never touches the
network (the HTTP fetcher is injected)."""

from src.scrapers.youtube_search import UFC_CHANNEL_ID, search_videos


def _fetcher(payload):
    """A fake HTTP fetcher that records the params and returns a fixed payload."""
    calls = []

    def fetch(params):
        calls.append(params)
        return payload

    fetch.calls = calls
    return fetch


def test_parses_items_into_videos():
    payload = {
        "items": [
            {
                "id": {"videoId": "abc123"},
                "snippet": {"title": "Free Fight: A vs B", "channelId": UFC_CHANNEL_ID},
            }
        ]
    }
    videos = search_videos("A vs B", "key", fetcher=_fetcher(payload))
    assert len(videos) == 1
    assert videos[0].video_id == "abc123"
    assert videos[0].title == "Free Fight: A vs B"
    assert videos[0].channel_id == UFC_CHANNEL_ID
    assert videos[0].url == "https://www.youtube.com/watch?v=abc123"


def test_skips_items_without_video_id():
    payload = {
        "items": [
            {"id": {}, "snippet": {"title": "no id"}},
            {"snippet": {"title": "missing id key"}},
            {"id": {"videoId": "keep"}, "snippet": {"title": "ok", "channelId": "c"}},
        ]
    }
    videos = search_videos("q", "key", fetcher=_fetcher(payload))
    assert [v.video_id for v in videos] == ["keep"]


def test_tolerates_missing_snippet():
    payload = {"items": [{"id": {"videoId": "x"}}]}
    videos = search_videos("q", "key", fetcher=_fetcher(payload))
    assert len(videos) == 1
    assert videos[0].title == ""
    assert videos[0].channel_id == ""


def test_empty_response_returns_empty_list():
    assert search_videos("q", "key", fetcher=_fetcher({})) == []
    assert search_videos("q", "key", fetcher=_fetcher({"items": []})) == []


def test_passes_query_and_channel_to_fetcher():
    fetcher = _fetcher({"items": []})
    search_videos("Jones vs Gane", "secret-key", fetcher=fetcher)
    sent = fetcher.calls[0]
    assert sent["q"] == "Jones vs Gane"
    assert sent["channelId"] == UFC_CHANNEL_ID
    assert sent["key"] == "secret-key"
