#!/usr/bin/env python3
"""Live pool cap. Re-probes orslot; does not freeze the launch-time guess."""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

from datetime import datetime, timedelta, timezone

from harness_lib import DEFAULT_WORKERS_PER_KEY, utc_now


Probe = Callable[[], tuple[int, int]]


def parse_slot_pool(text: str) -> tuple[int, int]:
    """Count only keys that still have remaining quota.

    Spent/overdrawn keys must not inflate the concurrency cap. Four keys with
    two spent is two live keys, not four. Pool totals ("today 1449/4000")
    and the current-slot banner are ignored.
    """
    live: list[tuple[int, int]] = []
    saw_key_line = False
    for line in text.splitlines():
        low = line.lower()
        stripped = line.strip()
        if stripped.lower().startswith("today") or stripped.lower().startswith("slot "):
            continue
        if "overdrawn" in low or "spent" in low:
            saw_key_line = True
            continue
        match = re.match(r"^\s*\*?\s*\d+\s+.*?(\d+)/(\d+)(?:\s|$)", line)
        if not match:
            continue
        saw_key_line = True
        used, cap = int(match.group(1)), int(match.group(2))
        remaining = cap - used
        if remaining > 0:
            live.append((used, cap))
    if not saw_key_line:
        return 1, 10**9
    remaining = sum(cap - used for used, cap in live)
    return len(live), max(0, remaining)


def midnight_window_sec() -> float:
    """Seconds before UTC midnight to stop launching. 0 disables. Default 600."""
    raw = os.environ.get("OCODEX_MIDNIGHT_WINDOW_SEC", "600")
    try:
        value = float(raw)
        if value >= 0:
            return value
    except ValueError:
        pass
    return 600.0


def seconds_to_utc_midnight(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    nxt = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
    return max(0.0, (nxt - now).total_seconds())


def pause_for_utc_reset(now: datetime | None = None) -> bool:
    window = midnight_window_sec()
    if window <= 0:
        return False
    return seconds_to_utc_midnight(now) <= window


def cap_refresh_sec() -> float:
    raw = os.environ.get("OCODEX_CAP_REFRESH_SEC", "30")
    try:
        value = float(raw)
        if value >= 0:
            return value
    except ValueError:
        pass
    return 30.0


class CapacityMonitor:
    """Mutable cap. Probe failures / one-off 50% key drops do not inflate."""

    def __init__(
        self,
        probe: Probe,
        *,
        workers_per_key: int = DEFAULT_WORKERS_PER_KEY,
        max_workers: int = 0,
        refresh_sec: float | None = None,
    ) -> None:
        self._probe = probe
        self.workers_per_key = max(1, int(workers_per_key))
        self.max_workers = max(0, int(max_workers or 0))
        self.refresh_sec = cap_refresh_sec() if refresh_sec is None else float(refresh_sec)
        self.keys = 0
        self.remaining = 0
        self._last = 0.0
        self._drop_strikes = 0
        self._primed = False
        self.history: list[dict[str, Any]] = []
        self.refresh(force=True)

    def snapshot(self) -> dict[str, Any]:
        return {
            "ts": utc_now(),
            "keys": self.keys,
            "remaining": self.remaining,
            "cap": self.cap(),
            "dry": self.remaining <= 0,
        }

    def cap(self) -> int:
        if self.remaining <= 0:
            return 0
        keys = max(1, self.keys)
        recommended = keys * self.workers_per_key
        if self.max_workers:
            return max(1, self.max_workers)
        return recommended

    def refresh(self, force: bool = False, now: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        if not force and self.refresh_sec > 0 and now - self._last < self.refresh_sec:
            return self.snapshot()
        try:
            keys, remaining = self._probe()
            keys = int(keys)
            remaining = int(remaining)
        except Exception:
            self._last = now
            return self.snapshot()
        if self._primed and self.keys and keys < self.keys * 0.5 and keys >= 0:
            self._drop_strikes += 1
            self._last = now
            if self._drop_strikes < 2:
                return self.snapshot()
        else:
            self._drop_strikes = 0
        self._primed = True
        self.keys = max(0, keys)
        self.remaining = max(0, remaining)
        self._last = now
        snap = self.snapshot()
        self.history.append(snap)
        if len(self.history) > 64:
            self.history = self.history[-64:]
        return snap

    def line(self) -> str:
        snap = self.snapshot()
        return (
            f"pool {snap['keys']} live key(s), {snap['remaining']} req left, "
            f"cap {snap['cap']}"
            + ("  [DRY — not launching]" if snap["dry"] else "")
            + ("  [UTC reset pause]" if pause_for_utc_reset() else "")
        )
