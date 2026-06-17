from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .config import Settings


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    url: str
    html: str
    soup: BeautifulSoup


class UfcStatsClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
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

    def _get_with_challenge_handling(self, url: str) -> tuple[requests.Response, str]:
        last_html = ""
        for attempt in range(3):
            self._respect_rate_limit()
            response = self._session.get(url, timeout=self._settings.request_timeout_seconds)
            response.raise_for_status()
            html = response.text
            last_html = html
            if not self._looks_like_challenge(html):
                return response, html
            LOGGER.info(
                "Encountered anti-bot challenge for %s on attempt %s; attempting challenge solve.",
                url,
                attempt + 1,
            )
            self._solve_challenge(response.url, html)
            backoff_seconds = min(2**attempt, 4)
            time.sleep(backoff_seconds)
        raise RuntimeError(f"Unable to bypass UFCStats anti-bot challenge for {url}")

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self._settings.request_delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
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