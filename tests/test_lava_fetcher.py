"""Regression tests for lava-india's HttpFetcher transport repair (2026-08-30).

Background: 3 of ~50 natural experimental-lane runs failed whole-run with a
single `ReadTimeout` on lavamobiles.com. Lava's own fetcher predates the
915f908 hmd fetcher repair: single attempt, no error handling at all, and
the body read (`resp.text`) outside any protected path — so one stalled
page aborted the entire crawl. This file pins the ported contract — all
against a mocked `requests.get` transport, never the live network (user
constraint 7). Contract is deliberately identical to
tests/test_http_fetcher.py (the 915f908 reference):

- transport failures (timeout / connection error) retried with exponential
  backoff, bounded by `max_attempts` (3);
- the body download is covered by the same error handling as the request;
- `get()` NEVER raises — exhausted retries surface as
  `FetchResult(status=0, error=<classification>)`;
- transient 429/5xx retried; a final 429/5xx returned verbatim;
- the (connect, read) timeout tuple and UA header reach the transport.
"""

from __future__ import annotations

import time

import pytest
import requests

from feature_phone_clank.collectors.lava import (
    DEFAULT_HTTP_TIMEOUT,
    HttpFetcher,
    _classify_network_error,
)


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _response_whose_body_raises(exc: Exception):
    """Mimics a requests.Response whose headers arrived but whose body
    download stalls: `.text` raises ReadTimeout mid-transfer."""
    class R:
        status_code = 200

        @property
        def text(self):
            raise exc

    return R()


class MockTransport:
    """Stands in for `requests.get`; each call consumes the next outcome.
    The last outcome repeats once exhausted."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.kwargs: list[dict] = []

    def __call__(self, url, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def captured_sleeps(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    return sleeps


def _read_timeout():
    return requests.exceptions.ReadTimeout(
        "HTTPSConnectionPool(host='www.lavamobiles.com', port=443): read timed out"
    )


def test_transient_read_timeout_is_retried_then_succeeds(monkeypatch, captured_sleeps):
    transport = MockTransport([
        _read_timeout(),
        FakeResponse(200, "fine"),
    ])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.3, backoff_s=2.0)

    result = fetcher.get("https://www.lavamobiles.com/featurephones?subCat=all")

    assert result.status == 200
    assert result.text == "fine"
    assert result.error == ""
    assert transport.calls == 2
    assert captured_sleeps == [0.3, 2.0, 0.3]


def test_persistent_timeout_exhausts_budget_and_fails_loudly(monkeypatch, captured_sleeps):
    transport = MockTransport([_read_timeout()])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.3, backoff_s=2.0, max_attempts=3)

    result = fetcher.get("https://www.lavamobiles.com/featurephones?subCat=all")

    assert transport.calls == 3  # bounded: never unlimited
    assert result.status == 0
    assert result.error == "timeout"
    # backoff after attempts 1 and 2, plus the politeness delay per real request
    assert captured_sleeps == [0.3, 2.0, 0.3, 4.0, 0.3]


def test_body_download_timeout_is_caught_and_retried(monkeypatch, captured_sleeps):
    """THE pre-repair lava root cause: `.text` evaluated outside the fetcher's
    error path escaped as an exception and failed the whole run. Must now be
    handled exactly like a request-level timeout."""
    transport = MockTransport([
        _response_whose_body_raises(_read_timeout()),
        FakeResponse(200, "recovered"),
    ])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.3, backoff_s=2.0)

    result = fetcher.get("https://www.lavamobiles.com/featurephones/hero600-pluse")

    assert result.status == 200
    assert result.text == "recovered"
    assert transport.calls == 2


def test_ordinary_healthy_fetch_is_unchanged(monkeypatch, captured_sleeps):
    transport = MockTransport([FakeResponse(200, "catalogue html")])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.3)

    result = fetcher.get("https://www.lavamobiles.com/featurephones?subCat=all")

    assert result.status == 200
    assert result.text == "catalogue html"
    assert result.error == ""
    assert transport.calls == 1  # no retry on a healthy response
    assert transport.kwargs[0]["timeout"] == DEFAULT_HTTP_TIMEOUT
    assert "FeaturePhoneClank/0.1" in transport.kwargs[0]["headers"]["User-Agent"]


def test_final_503_is_returned_verbatim_not_rewritten(monkeypatch, captured_sleeps):
    transport = MockTransport([FakeResponse(503, "down")])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.3, backoff_s=2.0, max_attempts=3)

    result = fetcher.get("https://www.lavamobiles.com/featurephones?subCat=all")

    assert transport.calls == 3  # transient status IS retried
    assert result.status == 503  # but the final status is honest evidence
    assert result.error == ""


def test_connection_error_classified_and_retried(monkeypatch, captured_sleeps):
    transport = MockTransport([
        requests.exceptions.ConnectionError("connection refused"),
        FakeResponse(200, "ok"),
    ])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.3, backoff_s=2.0)

    result = fetcher.get("https://www.lavamobiles.com/featurephones?subCat=all")

    assert result.status == 200
    assert result.error == ""
    assert _classify_network_error(requests, requests.exceptions.ConnectionError()) == "connection_error"
