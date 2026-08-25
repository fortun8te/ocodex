#!/usr/bin/env python3
"""Read a batch out-dir and produce a snapshot + terminal table.

Used by the launcher (status.json / status.txt every poll) and by
fleet_watch.py. Pure reads plus the snapshot writers — no process control.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read(path: Path, limit: int = 8000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _tail_lines(text: str, n: int = 6) -> list[str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-n:]


def parse_checkpoint(text: str) -> dict[str, Any]:
    """Pull Done / Files touched / Next step / Open questions from progress.md."""
    sections = {"done": [], "files": [], "next": "", "questions": []}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        key = line.strip().lower()
        if key.startswith("## done"):
            current = "done"
            continue
        if key.startswith("## files"):
            current = "files"
            continue
        if key.startswith("## next"):
            current = "next"
            continue
        if key.startswith("## open"):
            current = "questions"
            continue
        if current is None or not line.strip():
            continue
        if current == "next":
            if not sections["next"] and not line.strip().startswith("#"):
                sections["next"] = line.strip()
        elif current == "done":
            sections["done"].append(line.strip())
        elif current == "files":
            sections["files"].append(line.strip())
        elif current == "questions":
            sections["questions"].append(line.strip())
    if not sections["next"]:
        leftover = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if leftover:
            sections["next"] = leftover[-1]
    return sections


def _goal(task: str, width: int = 68) -> str:
    line = " ".join(task.strip().split())
    return line if len(line) <= width else line[: width - 1] + "…"


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _fmt_ago(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return _fmt_dur(seconds) + " ago"


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


_load_json = load_json


def _live_now(live: dict[str, Any]) -> str:
    """Best-effort 'what is it doing' from Codex stderr/stdout tails."""
    for key in ("stderr_tail", "stdout_tail"):
        lines = live.get(key) or []
        if isinstance(lines, str):
            lines = _tail_lines(lines)
        interesting = [
            ln for ln in lines
            if ln and not ln.startswith("ocodex:")
            and "device not configured" not in ln
            and not ln.startswith("hook:")
            and not ln.startswith("OpenAI Codex")
            and not ln.startswith("--------")
            and not ln.startswith("workdir:")
            and not ln.startswith("model:")
            and not ln.startswith("provider:")
            and not ln.startswith("approval:")
            and not ln.startswith("sandbox:")
            and not ln.startswith("session id:")
            and not ln.startswith("reasoning")
            and ln.strip() not in {"user", "codex", "tokens used"}
        ]
        if interesting:
            return interesting[-1][:80]
    return ""


def _marker_state(out: Path, name: str) -> str | None:
    if (out / f"{name}-retry.failed").exists() or (out / f"{name}.failed").exists():
        return "failed"
    if (out / f"{name}-retry.done").exists() or (out / f"{name}.done").exists():
        return "ok"
    return None


def collect_state(
    out: Path,
    jobs: list[dict[str, Any]] | None = None,
    *,
    cap: int = 0,
    started_at: float | None = None,
    events: list[str] | None = None,
) -> dict[str, Any]:
    """Build a snapshot.

    `jobs` is the launcher's live view (name, task, mode, attempt, state,
    started_at, after). When omitted, reconstruct from ledger + markers.
    """
    out = Path(out)
    now = time.time()
    if jobs is None:
        jobs = _jobs_from_disk(out)
    agents = []
    counts = {"queued": 0, "running": 0, "retrying": 0, "ok": 0, "failed": 0, "blocked": 0, "stalled": 0}
    for job in jobs:
        name = job["name"]
        progress_path = out / f"{name}.progress.md"
        live_path = out / job.get("tag", name) / "live.json"
        if not live_path.exists():
            # runner writes live.json inside the tag dir (or a batch-* child)
            matches = list((out / job.get("tag", name)).glob("**/live.json")) if (out / job.get("tag", name)).exists() else []
            live_path = matches[0] if matches else live_path
        progress_text = _read(progress_path)
        parsed = parse_checkpoint(progress_text) if progress_text else {"next": "", "done": []}
        live = _load_json(live_path)
        state = job.get("state") or _marker_state(out, name) or "queued"
        started = job.get("started_at")
        seconds = (now - started) if started else job.get("seconds")
        mtime = _mtime(progress_path)
        last_update_s = (now - mtime) if mtime else None
        now_text = parsed.get("next") or _live_now(live) or {
            "queued": "waiting for a slot",
            "blocked": "waiting on " + ", ".join(job.get("after") or []) or "a dependency",
            "running": "starting…",
            "retrying": "resuming from checkpoint",
            "stalled": "no checkpoint heartbeat",
            "ok": "done",
            "failed": "died (see checkpoint)",
        }.get(state, state)
        row = {
            "name": name,
            "mode": job.get("mode") or "scout",
            "goal": job.get("goal") or _goal(job.get("task") or ""),
            "state": state,
            "attempt": job.get("attempt") or 1,
            "seconds": None if seconds is None else round(seconds, 1),
            "last_update_s": None if last_update_s is None else round(last_update_s, 1),
            "now": now_text,
            "after": job.get("after") or [],
        }
        agents.append(row)
        counts[state] = counts.get(state, 0) + 1
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "out": str(out),
        "started_at": started_at,
        "elapsed_s": None if not started_at else round(now - started_at, 1),
        "cap": cap,
        "counts": counts,
        "agents": agents,
        "events": events or [],
    }


def _jobs_from_disk(out: Path) -> list[dict[str, Any]]:
    ledger = _load_json(out / "ledger.json")
    seen: dict[str, dict[str, Any]] = {}
    for entry in ledger.get("jobs") or []:
        name = entry.get("agent")
        if not name:
            continue
        seen[name] = {
            "name": name,
            "attempt": entry.get("attempt") or 1,
            "tag": f"{name}-retry" if (entry.get("attempt") or 1) > 1 else name,
            "mode": entry.get("mode"),
            "task": entry.get("task") or "",
            "after": entry.get("after") or [],
        }
    for manifest in sorted(out.glob("*.manifest.json")):
        data = _load_json(manifest)
        for agent in data.get("agents") or []:
            name = agent.get("name")
            if not name:
                continue
            row = seen.setdefault(name, {"name": name, "attempt": 1, "tag": name})
            row.setdefault("task", agent.get("task") or "")
            row.setdefault("mode", agent.get("mode"))
            row.setdefault("after", agent.get("after") or [])
    jobs = list(seen.values())
    for job in jobs:
        marker = _marker_state(out, job["name"])
        job["state"] = marker or ("retrying" if job.get("attempt", 1) > 1 else "running")
    return jobs


def render_table(snapshot: dict[str, Any]) -> str:
    agents = snapshot.get("agents") or []
    counts = snapshot.get("counts") or {}
    live = counts.get("running", 0) + counts.get("retrying", 0) + counts.get("stalled", 0)
    cap = snapshot.get("cap") or 0
    elapsed = _fmt_dur(snapshot.get("elapsed_s"))
    header = (
        f"ocodex  {live}/{len(agents)} live"
        + (f"  cap {cap}" if cap else "")
        + f"  elapsed {elapsed}"
        + f"  ok {counts.get('ok', 0)}  fail {counts.get('failed', 0)}"
        + f"  queued {counts.get('queued', 0)}"
    )
    if not agents:
        return header + "\n(no agents yet)\n"
    name_w = max(4, min(20, max(len(a["name"]) for a in agents)))
    cols = f"{'#':>2}  {'NAME':<{name_w}}  {'STATE':<9}  {'FOR':>7}  {'UPDATED':>11}  NOW"
    lines = [header, cols, "-" * min(110, max(len(header), len(cols)))]
    for i, agent in enumerate(agents, 1):
        now_text = (agent.get("now") or "").replace("\n", " ")
        lines.append(
            f"{i:>2}  {agent['name']:<{name_w}}  {agent['state']:<9}  "
            f"{_fmt_dur(agent.get('seconds')):>7}  {_fmt_ago(agent.get('last_update_s')):>11}  "
            f"{now_text}"
        )
        goal = agent.get("goal") or ""
        if goal:
            pad = 2 + 2 + name_w + 2 + 9 + 2 + 7 + 2 + 11 + 2
            lines.append(" " * pad + f"goal: {goal}")
    events = snapshot.get("events") or []
    if events:
        lines.append("")
        lines.append("recent:")
        lines.extend(f"  {ev}" for ev in events[-8:])
    return "\n".join(lines) + "\n"


def write_status(out: Path, snapshot: dict[str, Any]) -> None:
    out = Path(out)
    (out / "status.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    (out / "status.txt").write_text(render_table(snapshot))
