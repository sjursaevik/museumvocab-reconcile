"""Retry/backoff and the rate-limit tripwire.

The dangerous failure mode is a 429/5xx being swallowed and recorded as an empty
result, permanently mislabelling a good term as no_match. These pin that:
  * transient statuses retry, then succeed;
  * a persistent HTTP failure RAISES (so the term is recorded ERROR and retried),
    rather than returning zero hits;
  * a genuine 200-with-no-matches returns cleanly (does NOT raise).
"""
from __future__ import annotations

import time

import pytest
import requests

from museumvocab_reconcile.adapters.aat import AatAdapter


class FakeResp:
    def __init__(self, status=200, json_data=None, headers=None):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json


class FakeSession:
    """Returns queued responses (or raises queued exceptions) in order."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Keep the retry tests instant.
    monkeypatch.setattr(time, "sleep", lambda *_: None)


def _adapter(session, **kw):
    return AatAdapter(cache=None, session=session, backoff=0, **kw)


# ---- base request() retry/backoff -----------------------------------------

def test_request_retries_transient_then_succeeds():
    session = FakeSession([FakeResp(429), FakeResp(200, {"ok": True})])
    adapter = _adapter(session, max_retries=2)
    resp = adapter.request("GET", "http://example/x")
    assert resp.json() == {"ok": True}
    assert session.calls == 2


def test_request_raises_after_persistent_5xx():
    session = FakeSession([FakeResp(503), FakeResp(503), FakeResp(503)])
    adapter = _adapter(session, max_retries=2)
    with pytest.raises(requests.HTTPError):
        adapter.request("GET", "http://example/x")
    assert session.calls == 3   # initial + 2 retries


def test_request_retries_connection_error_then_succeeds():
    session = FakeSession([requests.ConnectionError("boom"), FakeResp(200, {"ok": 1})])
    adapter = _adapter(session, max_retries=2)
    assert adapter.request("GET", "http://example/x").json() == {"ok": 1}


def test_wait_honours_retry_after_header_capped():
    adapter = _adapter(FakeSession([]))
    assert adapter._wait(FakeResp(429, headers={"Retry-After": "10"}), 0) == 10.0
    assert adapter._wait(FakeResp(429, headers={"Retry-After": "100"}), 0) == 30.0


# ---- reconcile() must raise on failure, not swallow -----------------------

def test_reconcile_raises_when_every_style_fails(monkeypatch):
    # Both request styles fail at the HTTP level -> reconcile must raise so the
    # term is recorded ERROR, never silently logged as no_match.
    adapter = AatAdapter(cache=None)

    def boom(*a, **k):
        raise requests.ConnectionError("rate limited")

    monkeypatch.setattr(adapter, "request", boom)
    with pytest.raises(requests.RequestException):
        adapter._reconcile({"q0": {"query": "x"}})


def test_reconcile_returns_zero_hits_without_raising(monkeypatch):
    # A real 200 with no matches must return cleanly (no_match), not raise.
    adapter = AatAdapter(cache=None)
    monkeypatch.setattr(adapter, "request", lambda *a, **k: FakeResp(200, {"q0": {"result": []}}))
    data = adapter._reconcile({"q0": {"query": "x"}})
    from museumvocab_reconcile.adapters.aat import _result_hits
    assert _result_hits(data, "q0") == []


def test_reconcile_remembers_working_style(monkeypatch):
    adapter = AatAdapter(cache=None)
    monkeypatch.setattr(
        adapter, "request",
        lambda *a, **k: FakeResp(200, {"q0": {"result": [{"id": "aat/300000001", "score": 9}]}}),
    )
    adapter._reconcile({"q0": {"query": "x"}})
    assert adapter._recon_style == "json"   # first style that returned hits
