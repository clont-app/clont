"""Tiny stdlib HTTP helper so webhook channels need no extra dependency."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

_MAX_RESPONSE_BYTES = 5_000_000  # cap the reply we'll parse, so a runaway server can't OOM us


def post_json(url: str, payload: dict[str, Any], timeout: float = 10.0) -> int:
    """POST `payload` as JSON to `url`, return the HTTP status code."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted webhook URL)
        return resp.status


def post_json_recv(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:

    """POST `payload` as JSON and return `(status, parsed_json_body)`.

    Unlike `post_json` this reads the response so the API uplink can receive
    server-derived events on the reply. Non-2xx responses raise (sanitized to the
    status code only — the URL/key are never put in the message), and the body we
    parse is capped so a runaway server can't exhaust memory.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-configured server URL)
            raw = resp.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError(f"server response exceeded {_MAX_RESPONSE_BYTES} bytes")
            body = json.loads(raw) if raw else {}
            return resp.status, body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as exc:
        # exc carries the request URL; re-raise with only the status so callers
        # that log the exception can't leak the endpoint (or anything else).
        raise RuntimeError(f"server returned HTTP {exc.code}") from None
