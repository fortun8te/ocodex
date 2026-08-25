#!/usr/bin/env python3
"""OpenAI-compatible provider table. OpenRouter is default; Groq is opt-in via key."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_PROVIDERS: list[dict[str, Any]] = [
    {
        "name": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "conc_limit": 6,
        "models": {
            "cheap": [
                "deepseek/deepseek-chat-v3.1:free",
                "qwen/qwen3-coder:free",
            ],
            "std": [
                "qwen/qwen3-coder:free",
                "deepseek/deepseek-chat-v3.1:free",
            ],
            "max": [],
        },
    },
    {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "conc_limit": 8,
        "models": {
            "cheap": ["llama-3.3-70b-versatile", "openai/gpt-oss-20b"],
            "std": ["llama-3.3-70b-versatile"],
            "max": ["openai/gpt-oss-120b"],
        },
    },
]


def providers_path() -> Path:
    raw = os.environ.get("OCODEX_PROVIDERS")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".ocodex" / "providers.json"


def load_providers(path: Path | str | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path else providers_path()
    if not target.is_file():
        return [dict(item) for item in DEFAULT_PROVIDERS]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [dict(item) for item in DEFAULT_PROVIDERS]
    if isinstance(data, dict):
        items = data.get("providers")
    else:
        items = data
    if not isinstance(items, list) or not items:
        return [dict(item) for item in DEFAULT_PROVIDERS]
    out: list[dict[str, Any]] = []
    for raw in items:
        if isinstance(raw, dict) and raw.get("name") and raw.get("base_url"):
            out.append(raw)
    return out or [dict(item) for item in DEFAULT_PROVIDERS]


def provider_available(provider: dict[str, Any]) -> bool:
    env_name = str(provider.get("key_env") or "")
    if provider.get("name") == "openrouter":
        return True
    if not env_name:
        return False
    return bool(os.environ.get(env_name, "").strip())


def find_provider(
    name: str | None,
    providers: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    table = providers if providers is not None else load_providers()
    if not name:
        return None
    needle = name.strip().lower()
    for item in table:
        if str(item.get("name", "")).lower() == needle:
            return item
    return None


def pick_provider(agent: dict[str, Any], providers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    table = providers if providers is not None else load_providers()
    named = find_provider(agent.get("provider"), table)
    if named:
        return named
    env_default = (os.environ.get("OCODEX_SCOUT_PROVIDER") if agent.get("mode") == "scout" else None) or os.environ.get("OCODEX_PROVIDER")
    if env_default:
        found = find_provider(env_default, table)
        if found and provider_available(found):
            return found
    for item in table:
        if item.get("name") == "openrouter":
            return item
    return table[0]


def default_tier(agent: dict[str, Any]) -> str:
    explicit = agent.get("tier")
    if explicit in {"cheap", "std", "max"}:
        return explicit
    return "cheap" if agent.get("mode") == "scout" else "std"


def model_candidates(
    agent: dict[str, Any],
    default_model: str | None = None,
    providers: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Explicit model wins. Else tier chain from the picked provider."""
    if agent.get("model"):
        return [str(agent["model"]).strip()]
    if default_model:
        return [default_model]
    provider = pick_provider(agent, providers)
    models = provider.get("models") or {}
    if not isinstance(models, dict):
        return []
    chain = list(models.get(default_tier(agent)) or [])
    seen: set[str] = set()
    out: list[str] = []
    for item in chain:
        if isinstance(item, str) and item.strip() and item not in seen:
            seen.add(item)
            out.append(item)
    return out
