"""Client selection in http_browser.fetch_url (no network, no real curl_cffi).

ESPN Deportes / ufcespanol.com block on TLS fingerprint, so production must use
curl_cffi's Chrome impersonation when the package is installed — and fall back
to plain requests (browser headers intact) when it is not. Both paths and the
lazy import seam are exercised here with fakes, so the suite passes identically
with and without curl_cffi installed.

CI now installs requirements-scrapers.txt (the file the 23 scraper crons run
on, and the only one carrying curl_cffi), so the real package is importable
there too — see the smoke test at the bottom, which is what would catch a pin
that stopped resolving.
"""

import builtins
import types

import pytest
import requests

from src.scrapers import http_browser


class FakeResponse:
    def __init__(self, status_code=200, content=b"ok"):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


class FakeCurlError(Exception):
    """Stand-in for curl_cffi's exceptions, which do NOT inherit from requests'."""


class FakeCurlResponse(FakeResponse):
    def raise_for_status(self):
        if self.status_code >= 400:
            raise FakeCurlError(f"HTTP {self.status_code}")


class FakeCurlRequests:
    """Minimal fake of the curl_cffi.requests module surface fetch_url uses."""

    def __init__(self, response=None, error=None):
        self.calls = []
        self._response = response or FakeCurlResponse()
        self._error = error

    def get(self, url, headers=None, timeout=None, impersonate=None):
        self.calls.append(
            {"url": url, "headers": headers, "timeout": timeout, "impersonate": impersonate}
        )
        if self._error is not None:
            raise self._error
        return self._response


def test_uses_curl_cffi_with_chrome_impersonation(monkeypatch):
    fake_curl = FakeCurlRequests(response=FakeCurlResponse(200, b"feed"))
    monkeypatch.setattr(http_browser, "_load_curl_cffi", lambda: fake_curl)
    monkeypatch.setattr(
        http_browser.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("requests.get must not run when curl_cffi is available"),
    )

    response = http_browser.fetch_url(
        "https://example.com/rss",
        headers={"User-Agent": "Mozilla/5.0 fake", "Accept": "application/rss+xml"},
        timeout=15,
    )

    assert response.content == b"feed"
    (call,) = fake_curl.calls
    assert call["impersonate"] == "chrome"
    assert call["timeout"] == 15
    # The impersonation must own the User-Agent (a mismatched UA re-triggers
    # the WAF); other headers pass through.
    assert call["headers"] == {"Accept": "application/rss+xml"}


def test_falls_back_to_requests_without_curl_cffi(monkeypatch):
    monkeypatch.setattr(http_browser, "_load_curl_cffi", lambda: None)
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return FakeResponse(200, b"feed")

    monkeypatch.setattr(http_browser.requests, "get", fake_get)

    headers = {"User-Agent": "Mozilla/5.0 fake", "Accept": "application/rss+xml"}
    response = http_browser.fetch_url("https://example.com/rss", headers=headers, timeout=15)

    assert response.content == b"feed"
    (call,) = calls
    # Fallback keeps the caller's headers as-is, User-Agent included.
    assert call["headers"] == headers
    assert call["timeout"] == 15


def test_load_curl_cffi_returns_none_when_not_installed(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("curl_cffi"):
            raise ImportError("No module named 'curl_cffi'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert http_browser._load_curl_cffi() is None


def test_load_curl_cffi_returns_requests_submodule_when_installed(monkeypatch):
    fake_pkg = types.SimpleNamespace(requests="curl-cffi-requests-module")
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "curl_cffi":
            return fake_pkg
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert http_browser._load_curl_cffi() == "curl-cffi-requests-module"


def test_curl_transport_errors_surface_as_requests_exceptions(monkeypatch):
    # Callers only catch requests.RequestException; curl_cffi's hierarchy does
    # not inherit from it, so fetch_url must translate.
    fake_curl = FakeCurlRequests(error=FakeCurlError("connection reset"))
    monkeypatch.setattr(http_browser, "_load_curl_cffi", lambda: fake_curl)

    with pytest.raises(requests.RequestException, match="connection reset"):
        http_browser.fetch_url("https://example.com/rss")


def test_programming_errors_escape_unwrapped(monkeypatch):
    # A TypeError (e.g. curl_cffi bump changing get()'s signature) must keep
    # its real traceback — wrapping it as RequestException would let per-feed
    # tolerance swallow a bug affecting every feed.
    fake_curl = FakeCurlRequests(error=TypeError("unexpected keyword argument"))
    monkeypatch.setattr(http_browser, "_load_curl_cffi", lambda: fake_curl)

    with pytest.raises(TypeError, match="unexpected keyword"):
        http_browser.fetch_url("https://example.com/rss")


def test_curl_http_error_status_surfaces_as_requests_exception(monkeypatch):
    fake_curl = FakeCurlRequests(response=FakeCurlResponse(403))
    monkeypatch.setattr(http_browser, "_load_curl_cffi", lambda: fake_curl)

    with pytest.raises(requests.RequestException, match="403"):
        http_browser.fetch_url("https://example.com/rss")


def test_fallback_http_error_still_raises(monkeypatch):
    monkeypatch.setattr(http_browser, "_load_curl_cffi", lambda: None)
    monkeypatch.setattr(
        http_browser.requests,
        "get",
        lambda url, headers=None, timeout=None: FakeResponse(403),
    )

    with pytest.raises(requests.HTTPError):
        http_browser.fetch_url("https://example.com/rss")


def test_check_status_false_returns_error_response(monkeypatch):
    fake_curl = FakeCurlRequests(response=FakeCurlResponse(403))
    monkeypatch.setattr(http_browser, "_load_curl_cffi", lambda: fake_curl)

    response = http_browser.fetch_url("https://example.com/rss", check_status=False)

    assert response.status_code == 403


def test_curl_cffi_realmente_importable_si_esta_instalado():
    """Smoke: the REAL package loads, with no fakes in the way.

    Everything above runs on doubles, so a curl_cffi pin that stopped resolving
    would leave this suite green while all 23 scraper crons died — ESPN Deportes
    and ufcespanol block on TLS fingerprint and need the impersonation. CI
    installs requirements-scrapers.txt, so there this asserts for real; on a
    machine without it, it skips.
    """
    curl = http_browser._load_curl_cffi()
    if curl is None:
        pytest.skip("curl_cffi no instalado (entorno sin requirements-scrapers.txt)")
    assert hasattr(curl, "get"), "curl_cffi cargó pero no expone .get()"
