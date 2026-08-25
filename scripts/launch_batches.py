#!/usr/bin/env python3
"""launch_batches.py — capacity-aware multi-batch ocodex launcher.

Takes ONE master manifest with any number of agents and:
  1. probes the orslot pool (keys x remaining budget) and caps concurrency
     at slots * WORKERS_PER_KEY (default 6);
  2. validates required fields and disjoint owns across editing workers;
  3. writes ledger.json (start timestamp, ownership, chunk plan) and creates
     <out>/<name>.progress.md immediately;
  4. injects a STEP ZERO CHECKPOINT into every task;
  5. chunks agents into sub-batches (<=6 each), launching them through the
     shared runner in waves that respect the concurrency cap;
  6. writes <out>/chunk-N.done markers and a final <out>/all.done.

Usage:
  python3 launch_batches.py master-manifest.json --out-dir /path/out
                            [--workers-per-key 6] [--max-workers N]
                            [--timeout 900]
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from harness_lib import (
    DEFAULT_WORKERS_PER_KEY,
    RUNNER_BATCH_CAP,
    ManifestError,
    checkpoint_clause,
    estimate_requests,
    format_headroom,
    load_manifest,
    new_ledger,
    plan_capacity,
    seed_progress_files,
    update_ledger_chunk,
    utc_now,
    validate_manifest,
    write_json,
)

SKILL_DIR = Path(__file__).resolve().parent
RUNNER = Path(os.environ.get("OCODEX_RUNNER", "")) if os.environ.get("OCODEX_RUNNER") else (
    SKILL_DIR / "run_agents.py"
    if (SKILL_DIR / "run_agents.py").exists()
    else Path.home() / ".claude/skills/ocodex/scripts/run_agents.py"
)
ORSLOT = Path(os.environ.get("ORSLOT_BIN", str(Path.home() / "bin/orslot")))
def parse_slot_pool(text: str) -> tuple[int, int]:
    """Count only keys that still have remaining quota.

    Spent/overdrawn keys must not inflate the concurrency cap. Four keys with
    two spent is two live keys, not four. Pool totals ("today 1449/4000")
    and the current-slot banner are ignored.
    """
    live: list[tuple[int, int]] = []
    for line in text.splitlines():
        low = line.lower()
        if "overdrawn" in low or "spent" in low:
            continue
        stripped = line.strip()
        if stripped.lower().startswith("today") or stripped.lower().startswith("slot "):
            continue
        match = re.match(r"^\s*\*?\s*\d+\s+.*?(\d+)/(\d+)(?:\s|$)", line)
        if not match:
            continue
        used, cap = int(match.group(1)), int(match.group(2))
        remaining = cap - used
        if remaining > 0:
            live.append((used, cap))
    remaining = sum(cap - used for used, cap in live)
    n_live = len(live)
    return (n_live if n_live else 1), max(0, remaining)


def slot_pool() -> tuple[int, int]:
    """(number of LIVE keys, requests remaining today across the pool)."""
    if not ORSLOT.exists():
        return 1, 10**9
    try:
        out = subprocess.run([str(ORSLOT)], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 1, 1000
    return parse_slot_pool(out)


def poll_interval() -> float:
    raw = os.environ.get("OCODEX_POLL_INTERVAL", "5")
    try:
        return max(0.05, float(raw))
    except ValueError:
        return 5.0


def inject_checkpoint(agent: dict, out: Path) -> None:
    clause = checkpoint_clause(out, agent["name"])
    task = agent.get("task") or ""
    if "STEP ZERO" in task:
        return
    agent["task"] = clause + "\n\n" + task


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--workers-per-key", type=int, default=DEFAULT_WORKERS_PER_KEY)
    ap.add_argument("--max-workers", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # A reused out dir carries stale done markers: all.done from the last run
    # would wake the supervisor before this run does any work.
    stale = [p.name for p in out.glob("*.done")]
    if stale:
        print(f"refusing to run: {', '.join(sorted(stale))} already exist in {out} "
              "(use a fresh out-dir or delete old markers)", file=sys.stderr)
        return 2

    try:
        master = load_manifest(Path(args.manifest))
        workdir, agents = validate_manifest(
            master, args.timeout, max_agents=None, require_workdir=True,
        )
    except ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # Keep original agent dicts (facts etc.) but use validated list as source of truth.
    master["agents"] = agents
    master["workdir"] = str(workdir)

    keys, remaining = slot_pool()
    est_need = estimate_requests(agents)
    plan = plan_capacity(
        len(agents), keys, remaining,
        workers_per_key=args.workers_per_key,
        max_workers=args.max_workers,
    )
    cap = plan["cap"]
    print(format_headroom(plan, est_need))
    if plan.get("over_recommend"):
        print("NOTE: concurrency exceeds recommended keys*workers/key (default 6/key). "
              "429s backoff/hop via orslot; this is allowed, not a crash. Do not jump to 20.",
              file=sys.stderr)

    for agent in agents:
        inject_checkpoint(agent, out)
    seed_progress_files(out, agents)

    chunk_size = plan["chunk_size"]
    chunks = [agents[i:i + chunk_size] for i in range(0, len(agents), chunk_size)]
    ledger = new_ledger(out=out, workdir=workdir, agents=agents, chunks=chunks)
    ledger["headroom"] = plan
    write_json(out / "ledger.json", ledger)
    running: list[tuple[int, subprocess.Popen]] = []
    logs: dict[int, object] = {}
    failed_chunks: list[int] = []
    launched = 0
    child_env = os.environ.copy()
    child_env["OCODEX_BATCH_OUT"] = str(out)
    child_env["OCODEX_STATS"] = child_env.get("OCODEX_STATS") or str(out / "stats.jsonl")

    def live_workers() -> int:
        return sum(len(chunks[i]) for i, p in running if p.poll() is None)

    def persist_ledger() -> None:
        write_json(out / "ledger.json", ledger)

    def mark_chunk(index: int, code: int | None, status: str) -> None:
        marker = out / f"chunk-{index}.done"
        if code is None:
            marker.write_text("killed")
        else:
            marker.write_text(str(code))
        update_ledger_chunk(ledger, index, status=status, exit_code=code)
        persist_ledger()

    def descendant_pids() -> list[int]:
        """Pids whose cmdline mentions this out-dir (ocodex grandchildren included)."""
        needle = str(out).encode()
        me = os.getpid()
        found: list[int] = []
        proc = Path("/proc")
        if not proc.exists():
            return found
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == me:
                continue
            try:
                cmd = (entry / "cmdline").read_bytes()
            except (OSError, PermissionError):
                continue
            if needle in cmd:
                found.append(pid)
        return found

    def kill_all() -> None:
        # Ctrl-C / kill of THIS launcher must not orphan runner children
        # or their start_new_session ocodex grandchildren.
        for _, p in running:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for pid in descendant_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.time() + 5
        for _, p in running:
            if p.poll() is None:
                remaining = max(0.05, deadline - time.time())
                try:
                    p.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        for pid in descendant_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def on_signal(signum, frame) -> None:
        kill_all()
        for log in list(logs.values()):
            try:
                log.close()
            except Exception:
                pass
        for i, p in running:
            code = p.poll()
            if not (out / f"chunk-{i}.done").exists():
                mark_chunk(i, code if code is not None else 128 + int(signum), "killed")
        ledger["killed_by"] = signum
        ledger["finished_at"] = utc_now()
        persist_ledger()
        (out / "all.done").write_text(f"killed:{signum}")
        sys.exit(128 + int(signum) if isinstance(signum, int) else 1)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    interval = poll_interval()
    while launched < len(chunks) or running:
        for i, p in running[:]:
            code = p.poll()
            if code is not None:
                log = logs.pop(i, None)
                if log is not None:
                    log.close()
                running.remove((i, p))
                if code != 0:
                    failed_chunks.append(i)
                    mark_chunk(i, code, "failed")
                    print(f"chunk-{i} FAILED (exit {code}); see chunk-{i}.runner.log")
                else:
                    mark_chunk(i, code, "ok")
                    print(f"chunk-{i} finished")
        while launched < len(chunks) and live_workers() + len(chunks[launched]) <= cap:
            i = launched
            sub = copy.deepcopy(master)
            sub["agents"] = chunks[i]
            mpath = out / f"chunk-{i}.manifest.json"
            mpath.write_text(json.dumps(sub, indent=2))
            log = open(out / f"chunk-{i}.runner.log", "w")
            logs[i] = log
            cmd = [sys.executable, str(RUNNER), str(mpath), "--out-dir", str(out / f"chunk-{i}")]
            if args.timeout:
                cmd.extend(["--timeout", str(args.timeout)])
            p = subprocess.Popen(
                cmd,
                stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
                env=child_env,
            )
            running.append((i, p))
            print(f"chunk-{i} launched ({len(chunks[i])} agents)")
            launched += 1
        time.sleep(interval)

    ledger["finished_at"] = utc_now()
    persist_ledger()
    summary = "failed:" + ",".join(map(str, failed_chunks)) if failed_chunks else "done"
    (out / "all.done").write_text(summary)
    if failed_chunks:
        print(f"all chunks complete WITH FAILURES ({summary}) — supervisor must finish those tasks")
    else:
        print("all chunks complete")
    return 1 if failed_chunks else 0


if __name__ == "__main__":
    sys.exit(main())
