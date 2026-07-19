"""Shared fixtures for the parse smoke tests (real network, zero DB).

These tests exist to catch SILENT parser breakage: ufc.com / ufcstats.com /
ESPN changing their HTML or API shape so a production parser starts returning
empty results without raising anything. Philosophy (do not "fix" a red smoke
test by weakening it into triviality):

  - assert on EMPTINESS or broken structure, never on exact values;
  - thresholds are deliberately lax (far below what the sources serve today);
  - anchors are immutable historical facts (a finished card, a fighter's
    already-fought record) so they cannot drift out from under the test;
  - every network download goes through ``retry_fetch`` so an HTTP/blocking/
    network failure is reported AS SUCH and is never confused with "the page
    arrived fine but the parser extracted nothing" (a broken selector), which
    each test asserts separately on the downloaded content.

No database is ever touched: Settings is built BY HAND below (never via
get_settings(), which requires DATABASE_URL) and the placeholder DSN would
fail loudly if any code path tried to connect.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

import pytest
import requests

from src.scrapers.config import Settings
from src.scrapers.enrich_ranked import _build_session as _build_espn_session
from src.scrapers.news import _RSS_HEADERS
from src.scrapers.rankings import BROWSER_HEADERS
from src.scrapers.upcoming_events import HOME_URL


T = TypeVar("T")

SMOKE_ATTEMPTS = 3
# Backoff between download attempts (seconds): transient blocks/ratelimits on
# these sites usually clear within half a minute.
SMOKE_BACKOFF_SECONDS = (20.0, 30.0)


@pytest.fixture(scope="session")
def smoke_settings() -> Settings:
    """Hand-built Settings: real request pacing, no DB, no API keys.

    The DSN is an unusable placeholder ON PURPOSE — smoke tests only exercise
    fetch + parse, and any accidental connect() would fail immediately.
    """
    return Settings(
        database_url="postgresql://smoke-unused",
        anthropic_api_key=None,
    )


@pytest.fixture(scope="session")
def retry_fetch() -> Callable[[str, Callable[[], T]], T]:
    """Wrap a network download in 3 attempts with ~20-30s backoff.

    ONLY for downloads — never wrap parse asserts in it. A final failure is
    reported as a network/blocking problem so it cannot be mistaken for a
    parser regression. RuntimeError is included because UfcStatsClient raises
    it when the anti-bot challenge cannot be solved (a blocking failure).

    ``attempts`` defaults to 3; clients that already retry internally (e.g.
    UfcStatsClient: transient retries x challenge loop) should pass
    ``attempts=1`` so retries do not NEST multiplicatively and hammer a source
    that is actively blocking.
    """

    def _retry(
        description: str,
        download: Callable[[], T],
        *,
        attempts: int = SMOKE_ATTEMPTS,
    ) -> T:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return download()
            except (requests.RequestException, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    wait = SMOKE_BACKOFF_SECONDS[
                        min(attempt, len(SMOKE_BACKOFF_SECONDS) - 1)
                    ]
                    time.sleep(wait)
        pytest.fail(
            f"NETWORK/HTTP/BLOCKING failure (NOT a parser break): {description} "
            f"still failing after {attempts} attempts: {last_error!r}"
        )

    return _retry


@pytest.fixture(scope="session")
def ufc_session(smoke_settings: Settings) -> requests.Session:
    """requests.Session with the exact browser headers production uses for
    ufc.com (rankings/upcoming_events BROWSER_HEADERS), warmed up with a home
    GET like upcoming_events.scrape_upcoming_events does before /events."""
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    try:
        session.get(HOME_URL, timeout=smoke_settings.request_timeout_seconds)
    except requests.RequestException:
        # Non-fatal: the real fetch below will surface (and retry) the failure.
        pass
    return session


@pytest.fixture(scope="session")
def espn_session(smoke_settings: Settings) -> requests.Session:
    """The production ESPN session (enrich_ranked._build_session, the same
    builder espn_fight_history uses; espn_live_results builds identical
    headers): Accept json + the repo UA pointed at espn.com."""
    return _build_espn_session(smoke_settings)


@pytest.fixture(scope="session")
def rss_session() -> requests.Session:
    """Session with the RSS headers production uses for feed fetches
    (news.py _RSS_HEADERS)."""
    session = requests.Session()
    session.headers.update(_RSS_HEADERS)
    return session
