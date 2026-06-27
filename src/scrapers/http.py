from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .config import Settings


LOGGER = logging.getLogger(__name__)

MAX_HTTP_RETRIES = 3
RETRY_BACKOFF_CAP_SECONDS = 30.0


@dataclass(frozen=True)
class FetchResult:
    url: str
    html: str
    soup: BeautifulSoup


class UfcStatsClient:
    def __init__(
        self,
        settings: Settings,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "http://ufcstats.com/",
                "Origin": "http://ufcstats.com",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        self._last_request_at = 0.0

    def fetch(self, url: str) -> FetchResult:
        response, html = self._get_with_challenge_handling(url)
        soup = BeautifulSoup(html, "lxml")
        return FetchResult(url=response.url, html=html, soup=soup)

    def fetch_all_pages(self, url: str) -> list[FetchResult]:
        pages: list[FetchResult] = []
        seen_page_keys: set[str] = set()
        next_url: str | None = url
        while next_url:
            page = self.fetch(next_url)
            pages.append(page)
            page_key = self._page_key(page.url)
            if page_key in seen_page_keys:
                break
            seen_page_keys.add(page_key)
            next_url = self._extract_next_page_url(page.soup, page.url, seen_page_keys)
        return pages

    def _get_with_challenge_handling(self, url: str) -> tuple[requests.Response, str]:
        for attempt in range(3):
            response = self._request_with_retries(url)
            html = response.text
            if not self._looks_like_challenge(html):
                return response, html
            LOGGER.info(
                "Encountered anti-bot challenge for %s on attempt %s; attempting challenge solve.",
                url,
                attempt + 1,
            )
            self._solve_challenge(response.url, html)
            self._sleep(min(2**attempt, 4))
        raise RuntimeError(f"Unable to bypass UFCStats anti-bot challenge for {url}")

    def _request_with_retries(self, url: str) -> requests.Response:
        for attempt in range(MAX_HTTP_RETRIES):
            self._respect_rate_limit()
            try:
                response = self._session.get(
                    url, timeout=self._settings.request_timeout_seconds
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt + 1 >= MAX_HTTP_RETRIES:
                    raise
                wait = self._retry_wait(attempt)
                LOGGER.info(
                    "Transient error fetching %s (%s); retrying in %.1fs (attempt %s/%s).",
                    url,
                    exc.__class__.__name__,
                    wait,
                    attempt + 1,
                    MAX_HTTP_RETRIES,
                )
                self._sleep(wait)
                continue
            if self._is_retryable_status(response.status_code):
                if attempt + 1 >= MAX_HTTP_RETRIES:
                    response.raise_for_status()
                wait = self._retry_wait_for_response(response, attempt)
                LOGGER.info(
                    "Retryable HTTP %s fetching %s; retrying in %.1fs (attempt %s/%s).",
                    response.status_code,
                    url,
                    wait,
                    attempt + 1,
                    MAX_HTTP_RETRIES,
                )
                self._sleep(wait)
                continue
            response.raise_for_status()
            return response
        raise RuntimeError(f"Exhausted retries fetching {url}")

    def _is_retryable_status(self, status_code: int) -> bool:
        return status_code == 429 or status_code >= 500

    def _retry_wait(self, attempt: int) -> float:
        return float(min(2**attempt, RETRY_BACKOFF_CAP_SECONDS))

    def _retry_wait_for_response(
        self, response: requests.Response, attempt: int
    ) -> float:
        if response.status_code == 429:
            retry_after = self._retry_after_seconds(response)
            if retry_after is not None:
                # Cap the server-provided Retry-After so a hostile/huge value
                # (e.g. 3600s) can't stall the scraper for an hour.
                return min(retry_after, RETRY_BACKOFF_CAP_SECONDS)
        return self._retry_wait(attempt)

    def _retry_after_seconds(self, response: requests.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        # Retry-After may also be an HTTP-date instead of a delta in seconds.
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if retry_at is None:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self._settings.request_delay_seconds - elapsed
        if remaining > 0:
            self._sleep(remaining)
        self._last_request_at = time.monotonic()

    def _looks_like_challenge(self, html: str) -> bool:
        return "Checking your browser" in html and 'xhr.open(\'POST\',"/__c"' in html

    def _solve_challenge(self, page_url: str, html: str) -> None:
        nonce_match = re.search(r'var nonce="([^"]+)"', html)
        zeros_match = re.search(r"target\s*=\s*new Array\((\d+)\+1\)\.join\('0'\)", html)
        if not nonce_match or not zeros_match:
            raise RuntimeError("Unable to parse UFCStats anti-bot challenge.")
        nonce = nonce_match.group(1)
        zeros = int(zeros_match.group(1))
        target = "0" * zeros
        prefix = f"{nonce}:"
        n = 0
        while hashlib.sha256(f"{prefix}{n}".encode("utf-8")).hexdigest()[:zeros] != target:
            n += 1
        challenge_url = urljoin(page_url, "/__c")
        response = self._session.post(
            challenge_url,
            data={"nonce": nonce, "n": n},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "http://ufcstats.com",
                "Referer": page_url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=self._settings.request_timeout_seconds,
        )
        response.raise_for_status()
        if self._looks_like_challenge(response.text):
            raise RuntimeError("Challenge solve POST returned another challenge page.")

    def _extract_next_page_url(
        self,
        soup: BeautifulSoup,
        current_url: str,
        seen_page_keys: set[str],
    ) -> str | None:
        current_page = self._page_number(current_url)
        candidate_urls: list[str] = []
        for link in soup.select("a[href]"):
            href = link.get("href")
            if not href:
                continue
            absolute_url = urljoin(current_url, href)
            if self._page_number(absolute_url) is None:
                continue
            candidate_urls.append(absolute_url)
        next_candidates = sorted(
            {
                candidate
                for candidate in candidate_urls
                if self._page_number(candidate) is not None
                and self._page_number(candidate) > current_page
                and self._page_key(candidate) not in seen_page_keys
            },
            key=self._page_number,
        )
        return next_candidates[0] if next_candidates else None

    def _page_number(self, url: str) -> int:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        raw_page = query.get("page", ["1"])[0]
        if raw_page == "all":
            return 1
        try:
            return int(raw_page)
        except ValueError:
            return 1

    def _page_key(self, url: str) -> str:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        page = query.get("page", ["1"])[0]
        normalized_query = urlencode({"page": page}) if page else ""
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", normalized_query, ""))