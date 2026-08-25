#!/usr/bin/env python3
"""Classify a worker result as ok / transient / timeout / stalled / fatal.

Unknown failures default to transient: the measured ~25% death mode is an
empty final after a provider stream drop, and one retry is cheap. Only skip
the retry when the tail is clearly a sandbox/auth/config problem.
"""
from __future__ import annotations

from typing import Any

FATAL_NEEDLES = (
    "sandbox violation",
    "permission denied",
    "operation not permitted",
    "invalid api key",
    "no key —",
    "ocodex executable not found",
    "counting proxy failed to start",
)

TRANSIENT_NEEDLES = (
    "broken pipe",
    "connection reset",
    "econnreset",
    "error sending",
    "stream closed",
    "socket hang up",
    "status 429",
    "status 502",
    "status 503",
    "status 504",
    "502 bad",
    "503 service",
    "internal server error",
    "cloudflare",
    "undici",
)


def classify(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "ok"
    if result.get("stalled"):
        return "stalled"
    if result.get("timed_out"):
        return "timeout"
    blob = "\n".join(result.get("error_tail") or []).lower()
    for needle in FATAL_NEEDLES:
        if needle in blob:
            return "fatal"
    for needle in TRANSIENT_NEEDLES:
        if needle in blob:
            return "transient"
    return "transient"


def should_retry(result: dict[str, Any], attempt: int) -> bool:
    """One retry, and only for failures that look recoverable."""
    if attempt != 0:
        return False
    return classify(result) != "fatal"
