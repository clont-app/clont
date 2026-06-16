"""post_json_recv: body parsing, response-size cap, sanitized non-2xx errors.

This is the API uplink's transport; the security-relevant behavior is that a
non-2xx response never leaks the endpoint URL and an oversized body can't OOM us.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from clont.channels import _http


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_parses_json_body(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(b'{"events": []}')
    )
    status, body = _http.post_json_recv("https://x", {"a": 1})
    assert status == 200
    assert body == {"events": []}


def test_empty_body_is_empty_dict(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(b""))
    assert _http.post_json_recv("https://x", {}) == (200, {})


def test_caps_response_size(monkeypatch):
    huge = b"x" * (_http._MAX_RESPONSE_BYTES + 10)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(huge))
    with pytest.raises(ValueError, match="exceeded"):
        _http.post_json_recv("https://x", {})


def test_non_2xx_is_sanitized_and_url_never_leaks(monkeypatch):
    secret_url = "https://secret.example.com/ingest?token=abc"

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(secret_url, 500, "Server Error", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as ei:
        _http.post_json_recv(secret_url, {})
    assert "500" in str(ei.value)
    assert "secret.example.com" not in str(ei.value)
