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
        "   HEARTBEAT <ISO-8601-Z> | <subtask> | <one-liner focus>\n"
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
        "   HEARTBEAT <ISO-8601-Z> | <subtask> | <one-liner focus>\n"
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


def append_stats(record: dict[str, Any], *paths: Path | str | None) -> None:
    """Append one JSON line to each path. Also to $OCODEX_STATS if set."""
    line = json.dumps(record, ensure_ascii=False)
    targets: list[Path] = []
    seen: set[str] = set()

    def add(raw: Path | str | None) -> None:
        if not raw:
            return
        path = Path(raw).expanduser()
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        targets.append(path)

    for raw in paths:
        add(raw)
    add(os.environ.get("OCODEX_STATS"))

    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read manifest: {exc}")
    if not isinstance(data, dict):
        fail("manifest must be a JSON object")
    return data


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def estimate_requests(agents: list[dict[str, Any]]) -> int:
    total = 0
    for agent in agents:
        mode = agent.get("mode", "scout")
        effort = agent.get("effort")
        table = REQUEST_GUESS.get(mode, REQUEST_GUESS["worker"])
        total += table.get(effort, table[None])
    return total


def validate_manifest(
    data: dict[str, Any],
    default_timeout: int = 900,
    *,
    max_agents: int | None = MAX_AGENTS,
    require_workdir: bool = True,
) -> tuple[Path, list[dict[str, Any]]]:
    """Validate required fields and disjoint owns across editing workers."""
    raw_workdir = data.get("workdir")
    if not isinstance(raw_workdir, str) or not raw_workdir:
        fail("manifest missing required field 'workdir' (non-empty path)")
    workdir = Path(raw_workdir).expanduser()
    if not workdir.is_absolute():
        workdir = Path.cwd() / workdir
    if require_workdir:
        if not workdir.is_dir():
            fail(f"workdir is not an existing directory: {workdir}")
        workdir = workdir.resolve()
    else:
        workdir = workdir.resolve() if workdir.exists() else workdir

    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        fail("manifest missing required field 'agents' (non-empty list)")
    if max_agents is not None and len(agents) > max_agents:
        fail(f"at most {max_agents} agents may run in one batch")

    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    owned: list[tuple[str, Path, str]] = []  # name, resolved, original

    for index, raw in enumerate(agents):
        if not isinstance(raw, dict):
            fail(f"agent {index} must be an object")
        name = raw.get("name")
        task = raw.get("task")
        mode = raw.get("mode", "scout")
        if name is None or name == "":
            fail(f"agent {index} missing required field 'name'")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            fail(f"agent {index} has an invalid name")
        if name in names:
            fail(f"duplicate agent name: {name}")
        names.add(name)
        if not isinstance(task, str) or not task.strip():
            fail(f"agent {name} missing required field 'task' (non-empty string)")
        if mode not in {"scout", "worker"}:
            fail(f"agent {name} mode must be scout or worker")

        owns = raw.get("owns", [])
        if not isinstance(owns, list) or not all(isinstance(item, str) and item for item in owns):
            fail(f"agent {name} owns must be a list of paths")
        if mode == "worker" and not owns:
            fail(f"worker {name} needs at least one owned path")

        base = workdir.resolve() if workdir.exists() else workdir
        for item in owns:
            owned_path = (base / item).resolve() if not Path(item).is_absolute() else Path(item).resolve()
            if workdir.exists() and not owned_path.is_relative_to(base):
                fail(f"worker ownership must stay inside workdir: {item}")
            if mode == "worker":
                for prev_name, previous, prev_item in owned:
                    if _paths_overlap(owned_path, previous):
                        fail(
                            f"ownership overlap: worker {name!r} and worker {prev_name!r} "
                            f"both own {item} (conflicts with {prev_item})"
                        )
                owned.append((name, owned_path, item))

        timeout = raw.get("timeout", default_timeout)
        if not isinstance(timeout, int) or timeout < 30:
            fail(f"agent {name} timeout must be an integer of at least 30 seconds")
        model = raw.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            fail(f"agent {name} model must be a non-empty string")
        effort = raw.get("effort")
        if effort is not None and effort not in {"low", "medium", "high", "xhigh"}:
            fail(f"agent {name} effort must be low, medium, high, or xhigh")

        facts = raw.get("facts")
        if facts is not None and not isinstance(facts, (str, list)):
            fail(f"agent {name} facts must be a string or list of strings")

        normalized.append({
            "name": name,
            "task": task.strip(),
            "mode": mode,
            "owns": owns,
            "timeout": timeout,
            "model": model,
            "effort": effort,
            "facts": facts,
            "stop": raw.get("stop"),
        })
    return workdir if workdir.exists() else workdir, normalized


def new_ledger(
    *,
    out: Path,
    workdir: Path | str | None,
    agents: list[dict[str, Any]],
    chunks: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "started_at": utc_now(),
        "out": str(out),
        "workdir": str(workdir) if workdir else None,
        "ownership": {agent["name"]: list(agent.get("owns") or []) for agent in agents},
        "modes": {agent["name"]: agent.get("mode", "scout") for agent in agents},
        "models": {agent["name"]: agent.get("model") for agent in agents},
        "tasks": {
            agent["name"]: (strip_checkpoint(agent.get("task") or "").splitlines() or [""])[0][:100]
            for agent in agents
        },
        "chunk_plan": [
            {
                "chunk": index,
                "agents": [agent["name"] for agent in chunk],
                "status": "pending",
                "exit_code": None,
                "finished_at": None,
            }
            for index, chunk in enumerate(chunks)
        ],
        "killed_by": None,
    }


def seed_progress_files(out: Path, agents: list[dict[str, Any]]) -> None:
    """Create <out>/<name>.progress.md immediately so a crash still leaves a file."""
    stamp = utc_now()
    for agent in agents:
        path = out / f"{agent['name']}.progress.md"
        if path.exists():
            continue
        path.write_text(
            f"# {agent['name']} checkpoint\n\n"
            f"HEARTBEAT {stamp} | launched | harness created checkpoint; write analysis before edits\n"
            f"- harness created this file at launch ({stamp}). "
            "Write analysis here BEFORE editing any owned file. "
            "Then one bullet per named sub-task or per file touched. "
            "Heartbeat at least every 2 minutes.\n",
            encoding="utf-8",
        )


def update_ledger_chunk(
    ledger: dict[str, Any],
    chunk_index: int,
    *,
    status: str,
    exit_code: int | None,
) -> dict[str, Any]:
    for entry in ledger.get("chunk_plan", []):
        if entry.get("chunk") == chunk_index:
            entry["status"] = status
            entry["exit_code"] = exit_code
            entry["finished_at"] = utc_now()
            break
    return ledger


def merge_result_json(
    path: Path,
    *,
    agent: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Write/merge <name>.result.json. Preserve worker-supplied claims if present."""
    payload: dict[str, Any] = {
        "name": agent["name"],
        "status": "ok" if result.get("ok") else "failed",
        "files_touched": [],
        "claims": [],
        "failed_scenarios": [],
        "ok": bool(result.get("ok")),
        "return_code": result.get("return_code"),
        "seconds": result.get("seconds"),
        "error_class": result.get("error_class"),
        "retries": result.get("retries", 0),
        "timed_out": bool(result.get("timed_out")),
        "retrying": bool(result.get("retrying")),
    }
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            for key in ("status", "files_touched", "claims", "failed_scenarios"):
                if key in existing and existing[key] not in (None, [], ""):
                    payload[key] = existing[key]
            if result.get("ok") is False:
                payload["status"] = "failed"
            elif result.get("ok") is True and payload.get("status") not in {"ok", "partial"}:
                # Do not keep a harness retrying-marker status=failed after a successful retry.
                payload["status"] = "ok"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


DEFAULT_WORKERS_PER_KEY = 6
RUNNER_BATCH_CAP = 6


def slug_name(text: str, fallback: str = "worker") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    cleaned = cleaned[:40] or fallback
    if not NAME_RE.fullmatch(cleaned):
        cleaned = fallback
    return cleaned


def generate_manifest(
    goal: str,
    workdir: Path | str,
    *,
    owns: list[str] | None = None,
    name: str | None = None,
    mode: str | None = None,
    facts: list[str] | str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """Build a one-agent manifest from a short goal. Worker if owns else scout."""
    if not goal or not str(goal).strip():
        fail("goal must be a non-empty string")
    owns = [item for item in (owns or []) if item]
    resolved_mode = mode or ("worker" if owns else "scout")
    if resolved_mode not in {"scout", "worker"}:
        fail("mode must be scout or worker")
    if resolved_mode == "worker" and not owns:
        fail("worker mode needs at least one --owns path")
    agent: dict[str, Any] = {
        "name": name or slug_name(goal, "agent"),
        "mode": resolved_mode,
        "task": str(goal).strip(),
    }
    if owns:
        agent["owns"] = owns
    if facts:
        agent["facts"] = facts
    if effort:
        agent["effort"] = effort
    workdir_path = Path(workdir).expanduser()
    if not workdir_path.is_absolute():
        workdir_path = (Path.cwd() / workdir_path).resolve()
    else:
        workdir_path = workdir_path.resolve() if workdir_path.exists() else workdir_path
    return {"workdir": str(workdir_path), "agents": [agent]}


def plan_capacity(
    n_agents: int,
    keys: int,
    remaining: int,
    workers_per_key: int = DEFAULT_WORKERS_PER_KEY,
    max_workers: int = 0,
    runner_batch_cap: int = RUNNER_BATCH_CAP,
) -> dict[str, Any]:
    """Pool-aware concurrency. Ceiling is keys * ~6, not 'spawn more workers'."""
    keys = max(1, int(keys))
    workers_per_key = max(1, int(workers_per_key))
    recommended = keys * workers_per_key
    cap = recommended
    over_recommend = False
    if max_workers:
        cap = max(1, int(max_workers))
        over_recommend = cap > recommended
    # 6/key is rate-limit headroom, not a crash ceiling. Honour explicit caps.
    chunk_size = max(1, min(runner_batch_cap, cap))
    waves = (n_agents + chunk_size - 1) // chunk_size if n_agents else 0
    unused = max(0, cap - min(n_agents, cap))
    return {
        "keys": keys,
        "remaining": remaining,
        "workers_per_key": workers_per_key,
        "pool_cap": recommended,
        "recommended": recommended,
        "cap": cap,
        "over_recommend": over_recommend,
        "clamped_to_pool": False,
        "agents": n_agents,
        "chunk_size": chunk_size,
        "waves": waves,
        "unused_slots": unused,
    }



def format_headroom(plan: dict[str, Any], est_need: int) -> str:
    lines = [
        "headroom:",
        f"  keys:              {plan['keys']}",
        f"  workers/key:       {plan['workers_per_key']}  (OpenRouter slot-pool rule of thumb)",
        f"  pool recommended:  {plan['pool_cap']}  (keys * workers/key; default 6/key is rate-limit headroom, not a crash)",
        f"  concurrency cap:   {plan['cap']}"
        + ("  [over recommended — 429s backoff/hop; allowed, not a crash]" if plan.get("over_recommend") else ""),
        f"  agents in batch:   {plan['agents']}",
        f"  waves:             {plan['waves']}  (packing, not extra parallelism)",
        f"  unused slots now:  {plan['unused_slots']}",
        f"  reqs est:          ~{est_need}  (labeled guess, not measured)",
        f"  requests left:     ~{plan['remaining']}  (today; OpenRouter resets daily)",
        "ceiling: default ~6 concurrent workers per OpenRouter key (override --workers-per-key 8, not 20).",
        "quality gate: one supervisor. scaling: more keys via orslot, or pack waves. 429 is a hop, not a crash.",
    ]
    if plan["agents"] > plan["cap"]:
        lines.append(
            f"NOTE: {plan['agents']} agents and cap {plan['cap']} -> {plan['waves']} waves. "
            "That is packing, not a bigger fleet on one key."
        )
    if plan["remaining"] < est_need:
        lines.append("WARNING: pool may run dry mid-batch — fewer agents or `orslot add`.")
    return "\n".join(lines)


def ownership_lines(agents: list[dict[str, Any]]) -> list[str]:
    lines = []
    for agent in agents:
        owns = agent.get("owns") or []
        if agent.get("mode") == "scout" and not owns:
            desc = "read-only (scout)"
        else:
            desc = ", ".join(owns) if owns else "(none)"
        lines.append(f"  - {agent['name']} ({agent.get('mode', 'scout')}): {desc}")
    return lines


def supervisor_brief(
    *,
    out: Path | str,
    workdir: Path | str,
    agents: list[dict[str, Any]],
    skill_dir: Path | str | None = None,
    verify: str | None = None,
    facts: str | None = None,
    off_limits: str | None = None,
) -> str:
    """Filled SUPERVISOR.md spawn prompt — the management-layer tax cut."""
    skill = Path(skill_dir) if skill_dir else Path(__file__).resolve().parent.parent
    doctrine = skill / "SUPERVISOR.md"
    if not doctrine.exists():
        doctrine = Path.home() / ".claude/skills/ocodex/SUPERVISOR.md"
    wait_py = Path(__file__).resolve().parent / "wait_done.py"
    verify = verify or (
        "Run the real project build/tests yourself. Worker 'build passed' claims are not evidence."
    )
    facts = facts or "(none supplied — verify every claim against source)"
    off_limits = off_limits or (
        "anything not in this batch's ownership (see ledger.json); "
        "concurrent batches and the main loop are expected in git status"
    )
    owns = "\n".join(ownership_lines(agents)) or "  (no agents)"
    return "\n".join([
        "===== SUPERVISOR BRIEF (spawn one strong model with this) =====",
        f"Read {doctrine} and follow it exactly.",
        f"OUT={out}",
        f"Repo: {workdir}",
        "Workers and ownership:",
        owns,
        f"Ownership is also on disk: {out}/ledger.json",
        f"Concurrent work (git status, off-limits): {off_limits}",
        f"Real verification commands + expected results: {verify}",
        f"Facts that are authoritative: {facts}",
        "",
        "Wait (once, not a sleep loop):",
        f"  python3 {wait_py} {out} --timeout 1500",
        "Then parse <name>.result.json, git diff owned files, finish dead workers",
        "from <name>.progress.md, run the real build. Do not commit.",
        "===== END SUPERVISOR BRIEF =====",
    ])


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


def _load_stats_by_name(path: Path) -> dict[str, dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return by_name
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("name"):
                by_name[rec["name"]] = rec
    except OSError:
        return by_name
    return by_name


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def collect_status(out: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Build one status row per agent from ledger + progress + result + stats. No LLM."""
    now = now or datetime.now(timezone.utc)
    out = Path(out)
    ledger = _load_json(out / "ledger.json") or {}
    started = parse_iso(ledger.get("started_at"))
    elapsed = age_seconds(started, now)
    all_done = (out / "all.done").read_text(encoding="utf-8").strip() if (out / "all.done").exists() else ""
    stats = _load_stats_by_name(out / "stats.jsonl")
    modes = ledger.get("modes") or {}
    tasks = ledger.get("tasks") or {}
    models = ledger.get("models") or {}
    names: list[str] = []
    for entry in ledger.get("chunk_plan") or []:
        for name in entry.get("agents") or []:
            if name not in names:
                names.append(name)
    for name in list(modes) + list(tasks):
        if name not in names:
            names.append(name)
    # Also pick up progress files not in ledger (partial runs).
    for progress in sorted(out.glob("*.progress.md")):
        name = progress.name[: -len(".progress.md")]
        if name not in names:
            names.append(name)

    rows: list[dict[str, Any]] = []
    for name in names:
        progress = parse_progress(out / f"{name}.progress.md")
        result = _load_json(out / f"{name}.result.json") or {}
        rec = stats.get(name) or {}
        hb_ts = parse_iso(progress.get("heartbeat_ts"))
        hb_age = age_seconds(hb_ts, now)
        result_status = result.get("status")
        ok = result.get("ok")
        if ok is None:
            ok = rec.get("ok")
        retries = rec.get("retries") or result.get("retries") or 0
        finished_batch = bool(all_done)
        interval = heartbeat_interval_sec()
        if result_status == "ok" or ok is True:
            state = "done"
        elif result.get("retrying"):
            state = "retrying"
        elif result_status == "failed" or (finished_batch and ok is False):
            state = "dead"
        elif retries and not finished_batch and ok is False:
            state = "retrying"
        elif hb_age is not None and hb_age > interval:
            state = "STALE"
        elif hb_age is None and elapsed is not None and elapsed > interval:
            state = "STALE"
        elif finished_batch and not result and not rec:
            state = "dead"
        else:
            state = "alive"
        slot = rec.get("slot") or rec.get("key_slot") or "-"
        model = rec.get("model") or models.get(name) or "-"
        api = f"{model}" if slot == "-" else f"{model} / {slot}"
        focus = progress.get("focus") or tasks.get(name) or ""
        rows.append({
            "name": name,
            "mode": modes.get(name) or rec.get("mode") or "-",
            "focus": focus,
            "subtask": progress.get("subtask") or "-",
            "last_update": progress.get("last_update") or "",
            "elapsed": format_age(elapsed),
            "elapsed_sec": elapsed,
            "heartbeat": progress.get("heartbeat_ts") or "-",
            "heartbeat_age": format_age(hb_age),
            "heartbeat_age_sec": hb_age,
            "state": state,
            "api": api,
            "model": model,
            "slot": slot,
            "ok": ok,
            "retries": retries,
        })
    return rows


def format_status_table(rows: list[dict[str, Any]], *, out: Path | str | None = None) -> str:
    headers = ["NAME", "MODE", "STATE", "FOCUS", "SUBTASK", "LAST UPDATE", "ELAPSED", "HEARTBEAT", "AGE", "API/SLOT"]
    if not rows:
        body = "(no agents found — check ledger.json / *.progress.md)"
        title = f"ocodex slot board  out={out}" if out else "ocodex slot board"
        return title + "\n" + body

    def trunc(value: object, width: int) -> str:
        text = "" if value is None else str(value)
        text = " ".join(text.split())
        return text if len(text) <= width else text[: max(0, width - 1)] + "…"

    widths = [12, 6, 8, 22, 14, 24, 8, 20, 8, 18]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        hb = row.get("heartbeat") or "-"
        vals = [
            trunc(row.get("name"), widths[0]),
            trunc(row.get("mode"), widths[1]),
            trunc(row.get("state"), widths[2]),
            trunc(row.get("focus"), widths[3]),
            trunc(row.get("subtask"), widths[4]),
            trunc(row.get("last_update"), widths[5]),
            trunc(row.get("elapsed"), widths[6]),
            trunc(hb, widths[7]),
            trunc(row.get("heartbeat_age"), widths[8]),
            trunc(row.get("api"), widths[9]),
        ]
        lines.append("  ".join(v.ljust(w) for v, w in zip(vals, widths)))
    title = f"ocodex slot board  out={out}  heartbeat stale after {int(heartbeat_interval_sec())}s"
    return title + "\n" + "\n".join(lines)


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
