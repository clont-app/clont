"""Tiny stdlib HTTP helper so webhook channels need no extra dependency."""

from __future__ import annotations

import json
import urllib.request
from typing import Any


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
