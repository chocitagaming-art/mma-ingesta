"""RSS fetching for the news scraper (feed blocked in GitHub Actions).

Since 2026-06-30 the ESPN Deportes feed answers GitHub runners with an
empty/block body when fetched with feedparser's default User-Agent
("feedparser/6.x" from an Azure IP); feedparser parses that body to
bozo=False + 0 entries, so the run stayed GREEN with "fetched": 0.

The fetch now goes through requests with real-browser headers, HTTP errors
must surface (raise_for_status), and a run where EVERY feed yields zero
articles must fail loudly instead of silently succeeding. Everything is
mocked: no network.
"""

import pytest
import requests

from src.scrapers import news


VALID_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>ESPN Deportes - MMA</title>
    <item>
      <title>Islam Makhachev defiende el titulo</title>
      <link>https://espndeportes.espn.com/mma/nota/_/id/1</link>
      <description>Resumen uno</description>
      <pubDate>Wed, 01 Jul 2026 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Alex Pereira anuncia su regreso</title>
      <link>https://espndeportes.espn.com/mma/nota/_/id/2</link>
      <description>Resumen dos</description>
      <pubDate>Tue, 30 Jun 2026 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

# What a bot-block page looks like: HTML, HTTP 200, zero RSS entries.
BLOCK_HTML = b"<html><head><title>Access Denied</title></head><body>blocked</body></html>"


class FakeResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


def test_fetch_uses_browser_headers_and_returns_articles(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return FakeResponse(200, VALID_RSS)

    monkeypatch.setattr(news.requests, "get", fake_get)

    articles = news.fetch_feed_articles(max_articles=10)

    assert len(articles) == 2
    assert articles[0].title == "Islam Makhachev defiende el titulo"
    assert articles[0].url == "https://espndeportes.espn.com/mma/nota/_/id/1"
    # One explicit fetch per configured feed, with real-browser headers.
    assert len(calls) == len(news.RSS_FEEDS)
    headers = calls[0]["headers"]
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert "feedparser" not in headers["User-Agent"]
    assert "application/rss+xml" in headers["Accept"]
    assert calls[0]["timeout"] == 20


@pytest.mark.parametrize("body", [b"", BLOCK_HTML], ids=["empty-body", "block-html"])
def test_zero_articles_across_all_feeds_raises(monkeypatch, body):
    monkeypatch.setattr(
        news.requests,
        "get",
        lambda url, headers=None, timeout=None: FakeResponse(200, body),
    )
    with pytest.raises(RuntimeError, match="0 articles parsed from RSS feeds"):
        news.fetch_feed_articles(max_articles=10)


def test_http_403_surfaces_as_visible_error(monkeypatch):
    monkeypatch.setattr(
        news.requests,
        "get",
        lambda url, headers=None, timeout=None: FakeResponse(403),
    )
    with pytest.raises(requests.HTTPError):
        news.fetch_feed_articles(max_articles=10)
