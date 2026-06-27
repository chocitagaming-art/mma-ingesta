"""Retry behaviour for the UFCStats HTTP client (#22).

The session is mocked end to end so nothing touches the network, and the inter
attempt wait is injected via the ``sleep`` callable so the tests run instantly.
"""

from unittest import mock

import pytest
import requests

from src.scrapers.config import Settings
from src.scrapers.http import RETRY_BACKOFF_CAP_SECONDS, UfcStatsClient


class FakeResponse:
    def __init__(self, status_code, text="<html>ok</html>", url="http://ufcstats.com/page", headers=None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


def _make_client(sleep):
    settings = Settings(
        database_url="postgresql://test",
        anthropic_api_key=None,
        request_delay_seconds=0,
    )
    client = UfcStatsClient(settings, sleep=sleep)
    client._session = mock.Mock()
    return client


def test_retries_on_500_then_returns_200():
    sleep = mock.Mock()
    client = _make_client(sleep)
    client._session.get.side_effect = [FakeResponse(500), FakeResponse(200)]

    result = client.fetch("http://ufcstats.com/page")

    assert client._session.get.call_count == 2
    assert "ok" in result.html
    assert sleep.call_count >= 1


def test_exhausts_retries_on_persistent_5xx():
    sleep = mock.Mock()
    client = _make_client(sleep)
    client._session.get.side_effect = [FakeResponse(503), FakeResponse(503), FakeResponse(503)]

    with pytest.raises(requests.HTTPError):
        client.fetch("http://ufcstats.com/page")

    assert client._session.get.call_count == 3


def test_respects_retry_after_on_429():
    sleep = mock.Mock()
    client = _make_client(sleep)
    client._session.get.side_effect = [
        FakeResponse(429, headers={"Retry-After": "5"}),
        FakeResponse(200),
    ]

    result = client.fetch("http://ufcstats.com/page")

    assert client._session.get.call_count == 2
    assert "ok" in result.html
    sleep.assert_any_call(5.0)


def test_does_not_retry_on_404():
    sleep = mock.Mock()
    client = _make_client(sleep)
    client._session.get.side_effect = [FakeResponse(404), FakeResponse(200)]

    with pytest.raises(requests.HTTPError):
        client.fetch("http://ufcstats.com/page")

    assert client._session.get.call_count == 1
    sleep.assert_not_called()


def test_retries_on_timeout_then_returns_200():
    sleep = mock.Mock()
    client = _make_client(sleep)
    client._session.get.side_effect = [requests.Timeout("slow"), FakeResponse(200)]

    result = client.fetch("http://ufcstats.com/page")

    assert client._session.get.call_count == 2
    assert "ok" in result.html
    assert sleep.call_count >= 1


def test_caps_a_huge_retry_after_on_429():
    sleep = mock.Mock()
    client = _make_client(sleep)
    client._session.get.side_effect = [
        FakeResponse(429, headers={"Retry-After": "3600"}),
        FakeResponse(200),
    ]

    result = client.fetch("http://ufcstats.com/page")

    assert client._session.get.call_count == 2
    assert "ok" in result.html
    # No esperar la hora entera que pide el servidor: se capa al backoff máximo.
    sleep.assert_any_call(RETRY_BACKOFF_CAP_SECONDS)
    for call in sleep.call_args_list:
        assert call.args[0] <= RETRY_BACKOFF_CAP_SECONDS
