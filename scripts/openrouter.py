#!/usr/bin/env python3
"""Optional OpenRouter HTTP probes. Never logs the key. Failures are None."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _key() -> str:
    return (os.environ.get("OPENROUTER_API_KEY") or "").strip()


def fetch_json(
    path: str,
    *,
    api_key: str | None = None,
    timeout: float = 4.0,
    query: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    token = (api_key or _key()).strip()
    if not token:
        return None
    url = OPENROUTER_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def key_snapshot(api_key: str | None = None, timeout: float = 4.0) -> dict[str, Any] | None:
    """Credits / free-tier flag. No request-per-day count — OpenRouter does not expose it."""
    data = fetch_json("/key", api_key=api_key, timeout=timeout)
    if not data:
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    label = str(inner.get("label") or "")
    return {
        "is_free_tier": inner.get("is_free_tier"),
        "usage_daily": inner.get("usage_daily"),
        "usage": inner.get("usage"),
        "limit_remaining": inner.get("limit_remaining"),
        "limit": inner.get("limit"),
        "label_suffix": label[-4:] if label else "",
    }


def generation_snapshot(
    generation_id: str,
    api_key: str | None = None,
    timeout: float = 4.0,
) -> dict[str, Any] | None:
    """Real tokens/cost for one generation id. None if the CLI never surfaced an id."""
    gid = (generation_id or "").strip()
    if not gid:
        return None
    data = fetch_json("/generation", api_key=api_key, timeout=timeout, query={"id": gid})
    if not data:
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    return {
        "id": inner.get("id") or gid,
        "model": inner.get("model"),
        "tokens_prompt": inner.get("tokens_prompt") or inner.get("native_tokens_prompt"),
        "tokens_completion": inner.get("tokens_completion") or inner.get("native_tokens_completion"),
        "total_cost": inner.get("total_cost") or inner.get("usage"),
        "generation_time": inner.get("generation_time"),
    }
