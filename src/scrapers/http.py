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
            }
        )
        self._last_request_at = 0.0

    def fetch(self, url: str) -> FetchResult:
        self._respect_rate_limit()
        response = self._session.get(url, timeout=self._settings.request_timeout_seconds)
        response.raise_for_status()
        html = response.text
        if self._looks_like_challenge(html):
            LOGGER.info("Encountered anti-bot challenge for %s; attempting challenge solve.", url)
            self._solve_challenge(response.url, html)
            self._respect_rate_limit()
            response = self._session.get(url, timeout=self._settings.request_timeout_seconds)
            response.raise_for_status()
            html = response.text
        soup = BeautifulSoup(html, "lxml")
        return FetchResult(url=response.url, html=html, soup=soup)

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
        zeros_match = re.search(r"target=new Array\((\d+)\+1\)\.join\('0'\)", html)
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
            timeout=self._settings.request_timeout_seconds,
        )
        response.raise_for_status()