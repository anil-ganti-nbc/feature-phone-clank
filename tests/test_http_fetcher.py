"""Regression tests for HttpFetcher's timeout/retry policy (2026-08-29).

Background: 24 of 27 natural staging runs failed with a single uncaught
`ReadTimeout` raised while downloading a response BODY (`resp.text` was
evaluated outside the fetcher's except block, so one stalled page failed
the entire crawl). These tests pin the repaired contract — all against a
mocked `requests.get` transport, never the live network (user constraint 7):

- transport failures (timeout / connection error) are retried with
  exponential backoff, bounded by `max_attempts`;
- the body download is covered by the same error handling as the request
  itself (this exact scenario used to escape as an exception);
- `get()` NEVER raises — exhausted retries surface as
  `FetchResult(status=0, error=<classification>)`;
- transient 429/5xx answers are retried, but a final 429/5xx is returned
  verbatim (never rewritten to 0) because callers treat the HTTP status
  as evidence;
- the (connect, read) timeout tuple and UA header are actually passed to
  the transport.
"""

from __future__ import annotations

import time

import pytest
import requests

from feature_phone_clank.collectors.hmd import (
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
    download stalls: `.text` raises ReadTimeout (the production root
    cause)."""
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
    """Replace time.sleep with a recorder (restored after the test) so the
    backoff schedule can be asserted without actually sleeping."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    return sleeps


def _read_timeout():
    return requests.exceptions.ReadTimeout(
        "HTTPSConnectionPool(host='www.hmd.com', port=443): read timed out"
    )


def test_timeout_is_retried_then_succeeds(monkeypatch, captured_sleeps):
    transport = MockTransport([
        _read_timeout(),
        FakeResponse(200, "fine"),
    ])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.3, backoff_s=2.0)

    result = fetcher.get("https://www.hmd.com/en_int/feature-phones")

    assert result.status == 200
    assert result.text == "fine"
    assert result.error == ""
    assert transport.calls == 2
    # politeness delay before each real request + one exponential backoff
    assert captured_sleeps == [0.3, 2.0, 0.3]


def test_body_download_timeout_is_caught_and_retried(monkeypatch, captured_sleeps):
    """THE root-cause regression: a ReadTimeout raised while streaming the
    response body must be handled exactly like a request-level timeout —
    retried, never allowed to escape get(). Before the 2026-08-29 repair
    this scenario failed the entire run."""
    transport = MockTransport([
        _response_whose_body_raises(_read_timeout()),
        FakeResponse(200, "recovered"),
    ])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.0, backoff_s=0.0)

    result = fetcher.get("https://www.hmd.com/en_int/nokia-105-4g/specs")

    assert result.status == 200
    assert result.text == "recovered"
    assert transport.calls == 2


def test_body_download_timeout_on_every_attempt_returns_status_zero(
    monkeypatch, captured_sleeps,
):
    """Worst case of the root cause — every attempt stalls mid-body:
    get() must return a status-0 FetchResult (routing the caller into the
    same fallback path as any other failed page), never raise, and the
    error must be classified as a timeout."""
    monkeypatch.setattr(
        requests, "get",
        lambda url, **kwargs: _response_whose_body_raises(_read_timeout()),
    )
    fetcher = HttpFetcher(delay_s=0.0, backoff_s=0.0)

    result = fetcher.get("https://www.hmd.com/en_int/feature-phones")

    assert result.status == 0
    assert result.error == "timeout"


def test_exhausted_timeouts_are_bounded_with_exponential_backoff(
    monkeypatch, captured_sleeps,
):
    transport = MockTransport([_read_timeout()])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.3, backoff_s=2.0, max_attempts=3)

    result = fetcher.get("https://www.hmd.com/en_int/smartphones")

    assert result.status == 0
    assert result.error == "timeout"
    assert transport.calls == 3  # bounded — no infinite retry
    # delay before every request, backoff doubled between attempts:
    # 0.3 (req) 2.0 (backoff) 0.3 (req) 4.0 (backoff) 0.3 (req)
    assert captured_sleeps == [0.3, 2.0, 0.3, 4.0, 0.3]


def test_connection_errors_are_classified_distinctly(monkeypatch, captured_sleeps):
    transport = MockTransport([
        requests.exceptions.ConnectionError("reset by peer"),
        FakeResponse(200, "fine"),
    ])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.0, backoff_s=0.0)

    result = fetcher.get("https://www.hmd.com/en_int/sitemap-dtc.xml")

    assert result.status == 200
    assert transport.calls == 2

    # and when it never recovers, the classification says connection_error
    transport = MockTransport([requests.exceptions.ConnectionError("reset")])
    monkeypatch.setattr(requests, "get", transport)
    result = HttpFetcher(delay_s=0.0, backoff_s=0.0).get("https://www.hmd.com/x")
    assert result.status == 0
    assert result.error == "connection_error"


def test_transient_503_is_retried_then_succeeded(monkeypatch, captured_sleeps):
    """HMD's edge intermittently answers 503 (observed live on
    /en_int/sitemap-dtc.xml, 2026-08-29): retry, then accept the 200."""
    transport = MockTransport([
        FakeResponse(503, "service unavailable"),
        FakeResponse(200, "fine"),
    ])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.0, backoff_s=0.0)

    result = fetcher.get("https://www.hmd.com/en_int/sitemap-dtc.xml")

    assert result.status == 200
    assert result.error == ""
    assert transport.calls == 2


def test_persistent_503_is_returned_verbatim_not_rewritten(monkeypatch, captured_sleeps):
    """A final 429/5xx is honest server evidence: returned as-is (callers
    treat status as the source of truth), with no transport-error claim."""
    transport = MockTransport([FakeResponse(503, "service unavailable")])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher(delay_s=0.0, backoff_s=0.0)

    result = fetcher.get("https://www.hmd.com/en_int/sitemap-dtc.xml")

    assert result.status == 503
    assert result.error == ""
    assert transport.calls == 3  # still retried before giving up


def test_non_retryable_status_is_returned_immediately(monkeypatch, captured_sleeps):
    """404 (and other client errors) must not burn retries — the
    specs->base-page fallback depends on seeing the 404 cheaply."""
    transport = MockTransport([FakeResponse(404, "not found")])
    monkeypatch.setattr(requests, "get", transport)

    result = HttpFetcher(delay_s=0.0, backoff_s=0.0).get("https://www.hmd.com/en_int/x/specs")

    assert result.status == 404
    assert transport.calls == 1


def test_timeout_tuple_and_user_agent_reach_the_transport(monkeypatch, captured_sleeps):
    transport = MockTransport([FakeResponse(200, "ok")])
    monkeypatch.setattr(requests, "get", transport)
    fetcher = HttpFetcher()

    fetcher.get("https://www.hmd.com/en_int/feature-phones")

    assert transport.kwargs[0]["timeout"] == DEFAULT_HTTP_TIMEOUT
    assert transport.kwargs[0]["timeout"] == (10.0, 30.0)  # hard (connect, read) ceiling
    assert transport.kwargs[0]["headers"]["User-Agent"] == fetcher.user_agent


def test_classify_network_error_buckets():
    assert _classify_network_error(requests, _read_timeout()) == "timeout"
    assert _classify_network_error(
        requests, requests.exceptions.ConnectTimeout("connect")
    ) == "timeout"
    assert _classify_network_error(
        requests, requests.exceptions.ConnectionError("dns")
    ) == "connection_error"
    assert _classify_network_error(
        requests, requests.exceptions.RequestException("odd")
    ) == "network_error"
