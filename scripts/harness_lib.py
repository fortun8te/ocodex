#!/usr/bin/env python3
"""Shared ocodex harness helpers: validation, checkpoints, stats, ledger, briefs."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_AGENTS = 6
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
CHECKPOINT_MARK = "STEP ZERO"
HEARTBEAT_INTERVAL_SEC = 120
HEARTBEAT_RE = re.compile(
    r"^(?:-\s*)?HEARTBEAT\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*\|\s*(.*?)\s*\|\s*(.*)$"
)


def heartbeat_interval_sec() -> float:
    """Seconds without a HEARTBEAT before a live worker is treated as stale.

    Override with OCODEX_HEARTBEAT_SEC (tests use 1). Default 120.
    """
    raw = os.environ.get("OCODEX_HEARTBEAT_SEC")
    if raw is not None and str(raw).strip() != "":
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return float(HEARTBEAT_INTERVAL_SEC)


SEARXNG_DEFAULT_URL = "http://127.0.0.1:8080"

# Labeled guess, not a measurement. Distinguishes scout vs effort so waves
# don't all look like 40-req workers (SUGGESTIONS item 5 still a guess).
REQUEST_GUESS = {
    "scout": {"low": 15, "medium": 25, "high": 35, "xhigh": 50, None: 25},
    "worker": {"low": 30, "medium": 40, "high": 55, "xhigh": 80, None: 40},
}


class ManifestError(ValueError):
    """User-facing manifest problem; launchers map this to exit 2."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fail(message: str) -> None:
    raise ManifestError(message)


def checkpoint_clause(out: Path | str, name: str) -> str:
    """STEP ZERO of the task — not an addendum competing with the work."""
    progress = f"{out}/{name}.progress.md"
    return (
        "STEP ZERO (do this before anything else, including reading around):\n"
        f"1. Open `{progress}` (the harness already created it). "
        "If it is missing, create it immediately.\n"
        "2. Write your analysis into that checkpoint BEFORE editing any owned "
        "file: what you will change, why, and the failure scenario for each "
        "intended edit. No edits until that analysis is on disk.\n"
        "3. Then do the task. Append one checkpoint bullet per named sub-task "
        "or per file you touch — not one vague session bullet. Each bullet: "
        "what changed, which file, what you will do next.\n"
        "4. HEARTBEAT: at least every 2 minutes AND after every sub-task, append a "
        "line exactly of the form:\n"
        "   HEARTBEAT \u003cISO-8601-Z\u003e | \u003csubtask\u003e | \u003cone-liner focus\u003e\n"
        "   Example: HEARTBEAT 2026-08-25T16:04:00Z | parse-errors | reading src/parser.ts\n"
        "   If the last heartbeat is older than 2 minutes, the harness kills this "
        "process and auto-retries once from this checkpoint; a second stale is dead "
        "for the supervisor.\n"
        "If your process dies, that file is the only thing that survives; "
        "a supervisor will finish your task from it."
    )


def resume_clause(out: Path | str, name: str) -> str:
    """Retry prompt: continue from the existing checkpoint, do not start over."""
    progress = f"{out}/{name}.progress.md"
    return (
        "RESUME from the existing checkpoint — do not start over. "
        "Do not start the task over.\n"
        f"1. Open `{progress}` (already on disk). Read every HEARTBEAT and bullet. "
        "Do not rewrite the file from scratch.\n"
        "2. Continue from the last HEARTBEAT / last named sub-task. "
        "Skip work already recorded as done.\n"
        "3. Keep appending to the same file: one checkpoint bullet per named sub-task "
        "or per file you touch, plus HEARTBEAT lines.\n"
        "4. HEARTBEAT: at least every 2 minutes AND after every sub-task, append a "
        "line exactly of the form:\n"
        "   HEARTBEAT \u003cISO-8601-Z\u003e | \u003csubtask\u003e | \u003cone-liner focus\u003e\n"
        "Do not redo STEP ZERO analysis if it is already in the checkpoint."
    )


def strip_checkpoint(task: str) -> str:
    """Return the user goal with any injected STEP ZERO block removed."""
    text = task.strip()
    needle = "a supervisor will finish your task from it."
    if text.startswith(CHECKPOINT_MARK) or text.startswith("CHECKPOINT"):
        idx = text.find(needle)
        if idx == -1:
            return text
        return text[idx + len(needle):].strip()
    idx = text.find("\n\n" + CHECKPOINT_MARK)
    if idx == -1:
        idx = text.find("\n\nCHECKPOINT")
    if idx != -1:
        return text[:idx].strip()
    return text


def result_contract_blurb(result_path: Path | str) -> str:
    return (
        f"OUTPUT: when finished, write `{result_path}` as JSON with keys "
        'status (ok|partial|failed), files_touched (list of paths), '
        "claims (short strings you want the supervisor to verify), "
        "failed_scenarios (empty if none). Keep the final reply short: "
        "evidence, files touched, tests run, uncertainty."
    )


def compact_brief(
    agent: dict[str, Any],
    workdir: Path,
    *,
    checkpoint_out: Path,
    result_path: Path,
    resume: bool = False,
) -> str:
    """Worker prompt: goal, owns, facts, output shape, stop, checkpoint. No doctrine dump."""
    name = agent["name"]
    goal = strip_checkpoint(agent["task"])
    facts = agent.get("facts") or []
    if isinstance(facts, str):
        facts_lines = [facts] if facts.strip() else []
    else:
        facts_lines = [str(item) for item in facts if str(item).strip()]

    if agent["mode"] == "scout":
        owns_line = "OWNS: READ-ONLY. Do not edit files or mutate external systems."
    else:
        owned = ", ".join(agent["owns"])
        owns_line = (
            f"OWNS: {owned}. Edit only these paths. Do not revert others' work."
        )

    stop = agent.get("stop") or (
        "STOP when the goal is met, you would have to edit outside OWNS, "
        "or you are stuck. Do not wander or spawn subagents."
    )

    intro = resume_clause(checkpoint_out, name) if resume else checkpoint_clause(checkpoint_out, name)
    parts = [intro, ""]
    if resume:
        progress_path = Path(checkpoint_out) / f"{name}.progress.md"
        excerpt = ""
        if progress_path.exists():
            try:
                excerpt = "\n".join(progress_path.read_text(encoding="utf-8").splitlines()[-20:])
            except OSError:
                excerpt = ""
        if excerpt:
            parts.extend(["Last checkpoint excerpt:", excerpt, ""])
    parts.extend([
        "You are an ocodex worker. No parent conversation. Use only this brief and local evidence.",
        f"WORKDIR: {workdir}",
        f"NAME: {name}",
        f"MODE: {agent['mode']}",
        owns_line,
    ])
    if facts_lines:
        parts.append("FACTS (authoritative; do not contradict):")
        parts.extend(f"- {line}" for line in facts_lines)
    parts.extend([
        result_contract_blurb(result_path),
        f"STOP: {stop}",
        "",
        "TASK:",
        goal,
    ])
    return "\n".join(parts)


def classify_error(
    return_code: int | None,
    timed_out: bool,
    final_ok: bool,
    stderr_text: str = "",
    stale_heartbeat: bool = False,
) -> str | None:
    if stale_heartbeat:
        return "stale_heartbeat"
    if timed_out:
        return "timeout"
    if return_code == 0 and final_ok:
        return None
    err = (stderr_text or "").lower()
    if "stream" in err or "disconnect" in err:
        return "stream_fail"
    if not final_ok:
        return "empty_output"
    if return_code not in (0, None):
        return "crash"
    return "empty_output"


def parse_iso(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def age_seconds(ts: datetime | None, now: datetime | None = None) -> float | None:
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - ts).total_seconds())


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def parse_progress(path: Path) -> dict[str, Any]:
    """Pull last heartbeat, last subtask, last update, focus one-liner from a checkpoint."""
    info: dict[str, Any] = {
        "heartbeat_ts": None,
        "subtask": "",
        "focus": "",
        "last_update": "",
        "last_update_ts": None,
    }
    if not path.exists():
        return info
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return info
    last_hb = None
    last_text = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = HEARTBEAT_RE.match(stripped)
        if match:
            last_hb = match
            last_text = match.group(3).strip() or match.group(2).strip()
            continue
        last_text = stripped.lstrip("- ").strip()
    if last_hb:
        info["heartbeat_ts"] = last_hb.group(1)
        info["subtask"] = last_hb.group(2).strip()
        info["focus"] = last_hb.group(3).strip()
    info["last_update"] = last_text
    if path.exists():
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        info["last_update_ts"] = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")
    return info


def progress_is_stale(path: Path | str, now: datetime | None = None) -> bool:
    """True if last HEARTBEAT is older than the interval, or missing.

    The wait loop applies a grace period (attempt elapsed > interval) before
    treating a missing HEARTBEAT as stale. This helper itself only inspects
    the checkpoint file.
    """
    now = now or datetime.now(timezone.utc)
    interval = heartbeat_interval_sec()
    info = parse_progress(Path(path))
    hb_ts = parse_iso(info.get("heartbeat_ts"))
    if hb_ts is None:
        return True
    age = age_seconds(hb_ts, now)
    return age is None or age > interval


def is_stale_heartbeat(
    progress_path: Path,
    *,
    attempt_started: float,
    now: float | None = None,
    wall_now: datetime | None = None,
    interval: float | None = None,
) -> bool:
    """True if the attempt has run past `interval` and last HEARTBEAT is older than `interval`.

    Attempt duration uses a monotonic clock (`attempt_started` / `now`).
    Heartbeat age uses wall clock via progress_is_stale. Missing progress /
    missing HEARTBEAT counts as stale once the attempt itself has outlived
    the interval (the wait-loop grace period).
    """
    now = time.monotonic() if now is None else now
    interval = heartbeat_interval_sec() if interval is None else interval
    if now - attempt_started <= interval:
        return False
    return progress_is_stale(progress_path, now=wall_now)



def searxng_url() -> str:
    return (os.environ.get("SEARXNG_URL") or SEARXNG_DEFAULT_URL).rstrip("/")


def check_searxng(timeout: float = 2.0) -> tuple[bool, str, str]:
    """Return (ok, url, detail). Workers cannot web-search without this."""
    import urllib.error
    import urllib.request
    url = searxng_url()
    probe = url + "/"
    try:
        with urllib.request.urlopen(probe, timeout=timeout) as resp:
            code = getattr(resp, "status", 200)
            if 200 <= int(code) < 500:
                return True, url, f"HTTP {code}"
            return False, url, f"HTTP {code}"
    except Exception as exc:  # noqa: BLE001 — doctor must stay silent-catch
        return False, url, str(exc)


def searxng_next_command() -> str:
    return (
        "docker compose -f examples/searxng-compose.yml up -d\n"
        "  # or: docker run --name searxng -d -p 8080:8080 searxng/searxng:latest\n"
        "export SEARXNG_URL=http://127.0.0.1:8080"
    )


def _load_rest() -> None:
    from pathlib import Path as _RestPath
    _g = globals()
    _d = _RestPath(__file__).resolve().parent
    for _n in ("_harness_rest1.py", "_harness_rest2.py", "_harness_rest3.py"):
        _p = _d / _n
        exec(compile(_p.read_text(encoding="utf-8"), str(_p), "exec"), _g)


_load_rest()
