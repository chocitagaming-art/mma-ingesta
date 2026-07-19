"""Browser-impersonating HTTP fetch for hosts that block by TLS fingerprint.

Since ~2026-06-30 the ESPN Deportes CDN answers non-browser TLS fingerprints
with HTTP 202 + empty body (a SILENT failure: feedparser sees 0 entries and the
run stays green), and ufcespanol.com's WAF answers plain requests/curl with 403.
Both key on the TLS/JA3 fingerprint, so no amount of header spoofing with
`requests` helps. `curl_cffi` with ``impersonate="chrome"`` reproduces Chrome's
TLS + HTTP/2 fingerprint and both hosts answer 200 with the full feed.

``curl_cffi`` is imported lazily INSIDE the fetch: environments without the
dependency (CI unit tests install requirements.txt, which does not carry it)
fall back to plain ``requests`` with whatever headers the caller passes, so
nothing breaks — they just keep the old reachability (Marca only).

All transport/HTTP errors surface as ``requests.RequestException`` regardless
of which client ran, so callers keep a single exception surface.
"""

from __future__ import annotations

import logging

import requests


LOGGER = logging.getLogger(__name__)

# curl_cffi's rolling alias for its newest supported Chrome fingerprint; a
# pinned version (e.g. "chrome124") would silently age into a blockable one.
IMPERSONATE_TARGET = "chrome"


def _load_curl_cffi():
    """Return ``curl_cffi.requests`` if the package is installed, else None.

    Kept as a tiny seam (instead of importing at module top) so tests can
    monkeypatch it to force either client path deterministically, whether or
    not curl_cffi is installed in the environment running the suite.
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return None
    return curl_requests


def _drop_user_agent(headers: dict[str, str] | None) -> dict[str, str] | None:
    """Remove any caller-supplied User-Agent for the impersonated client.

    Impersonation must own the User-Agent: a UA that does not match the Chrome
    TLS fingerprint is exactly the browser-consistency check these WAFs run.
    Other headers (Accept, ...) pass through untouched.
    """
    if not headers:
        return None
    cleaned = {key: value for key, value in headers.items() if key.lower() != "user-agent"}
    return cleaned or None


def fetch_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    check_status: bool = True,
):
    """GET ``url`` impersonating Chrome when curl_cffi is available.

    Falls back to plain ``requests.get`` (with the caller's headers as-is) when
    curl_cffi is not installed. Returns the client's Response object — both
    clients expose ``.content``, ``.text``, ``.status_code``.

    Raises ``requests.RequestException`` on transport errors and (when
    ``check_status``) on HTTP >= 400, wrapping curl_cffi's own exception
    hierarchy, which does NOT inherit from requests'.
    """
    curl_requests = _load_curl_cffi()
    if curl_requests is None:
        response = requests.get(url, headers=headers, timeout=timeout)
        if check_status:
            response.raise_for_status()
        return response
    try:
        response = curl_requests.get(
            url,
            headers=_drop_user_agent(headers),
            timeout=timeout,
            impersonate=IMPERSONATE_TARGET,
        )
        if check_status:
            response.raise_for_status()
    except (TypeError, AttributeError, NameError):
        # Programming error (e.g. an API-breaking curl_cffi bump changes the
        # get() signature): let it escape with its real traceback instead of
        # masquerading as a per-feed network failure that per-feed tolerance
        # would swallow.
        raise
    except Exception as exc:
        # curl_cffi's transport/HTTP errors do NOT inherit from requests':
        # normalize so callers keep a single exception surface.
        raise requests.RequestException(f"impersonated fetch failed for {url}: {exc}") from exc
    return response
