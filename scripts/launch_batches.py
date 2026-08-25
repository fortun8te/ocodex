#!/usr/bin/env python3
"""launch_batches.py — capacity-aware multi-batch ocodex launcher.

Takes ONE master manifest with any number of agents and:
  1. probes the orslot pool (keys x remaining budget) and caps concurrency
     at slots * WORKERS_PER_KEY (default 6);
  2. validates required fields and disjoint owns across editing workers;
  3. writes ledger.json (start timestamp, ownership, chunk plan) and creates
     <out>/<name>.progress.md immediately;
  4. compact briefs (STEP ZERO lives in the runner prompt once, not in the task);
  5. work-stealing pool: one runner per agent, a free slot takes the next;
     live orslot refresh; dry pool defers leftovers instead of 429-storming;
  6. writes <out>/chunk-N.done markers, <out>/triage.json, and <out>/all.done.

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
import signal
import subprocess
import sys
import time
from pathlib import Path

from capacity import CapacityMonitor, parse_slot_pool, pause_for_utc_reset
from harness_lib import (
    DEFAULT_WORKERS_PER_KEY,
    ManifestError,
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


def slot_pool() -> tuple[int, int]:
    """(number of LIVE keys, requests remaining today across the pool)."""
    if not ORSLOT.exists():
        return 1, 10**9
    try:
        out = subprocess.run([str(ORSLOT)], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 1, 1000
    keys, remaining = parse_slot_pool(out)
    if keys == 0 and remaining == 0:
        return 0, 0
    return (keys if keys else 1), remaining


def poll_interval() -> float:
    raw = os.environ.get("OCODEX_POLL_INTERVAL", "5")
    try:
        return max(0.05, float(raw))
    except ValueError:
        return 5.0


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

    monitor = CapacityMonitor(
        slot_pool,
        workers_per_key=args.workers_per_key,
        max_workers=args.max_workers,
    )
    keys, remaining = monitor.keys, monitor.remaining
    est_need = estimate_requests(agents)
    plan = plan_capacity(
        len(agents), max(keys, 1) if remaining > 0 else 0, remaining,
        workers_per_key=args.workers_per_key,
        max_workers=args.max_workers,
    )
    plan["cap"] = monitor.cap()
    plan["keys"] = keys
    plan["remaining"] = remaining
    print(format_headroom(plan, est_need))
    print(monitor.line())
    if plan.get("over_recommend"):
        print("NOTE: concurrency exceeds recommended keys*workers/key (default 6/key). "
              "429s backoff/hop via orslot; this is allowed, not a crash. Do not jump to 20.",
              file=sys.stderr)
    try:
        from openrouter import key_snapshot
        snap = key_snapshot(timeout=3.0)
    except Exception:
        snap = None
    if snap:
        print(
            f"openrouter /key: free_tier={snap.get('is_free_tier')} "
            f"usage_daily={snap.get('usage_daily')} "
            f"limit_remaining={snap.get('limit_remaining')}"
        )

    seed_progress_files(out, agents)

    chunks = [[agent] for agent in agents]
    ledger = new_ledger(out=out, workdir=workdir, agents=agents, chunks=chunks)
    ledger["headroom"] = plan
    ledger["scheduler"] = "pool"
    if snap:
        ledger["openrouter"] = snap
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
    deferred: list[int] = []
    last_pool_line = ""
    while launched < len(chunks) or running:
        snap = monitor.refresh()
        cap = monitor.cap()
        line = monitor.line()
        if line != last_pool_line:
            print(line)
            last_pool_line = line
            ledger["headroom"] = {**plan, "cap": cap, "keys": monitor.keys, "remaining": monitor.remaining}
            persist_ledger()
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
        utc_pause = pause_for_utc_reset()
        launch_cap = 0 if utc_pause else cap
        if utc_pause and launched < len(chunks) and not running:
            print("UTC reset window — not launching until after midnight")
        while launched < len(chunks) and live_workers() + len(chunks[launched]) <= launch_cap:
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
            print(f"chunk-{i} launched ({chunks[i][0]['name']})")
            launched += 1
        if utc_pause and launched < len(chunks) and not running:
            time.sleep(interval)
            monitor.refresh(force=True)
            continue
        if launched < len(chunks) and cap <= 0 and not running:
            deferred = list(range(launched, len(chunks)))
            for i in deferred:
                mark_chunk(i, None, "deferred")
            print(
                f"pool dry — deferring {len(deferred)} agent(s): "
                + ", ".join(chunks[i][0]["name"] for i in deferred)
            )
            launched = len(chunks)
            break
        time.sleep(interval)

    ledger["finished_at"] = utc_now()
    persist_ledger()
    parts: list[str] = []
    if failed_chunks:
        parts.append("failed:" + ",".join(map(str, failed_chunks)))
    if deferred:
        parts.append("deferred:" + ",".join(map(str, deferred)))
    summary = ";".join(parts) if parts else "done"
    (out / "all.done").write_text(summary)
    try:
        from triage import write_triage
        tri = write_triage(out, workdir)
        print(f"triage: escalate={tri['counts']['escalate']} crashed={tri['counts']['crashed']} -> {out}/triage.json")
    except Exception as exc:  # noqa: BLE001 — triage must not fail the batch marker
        print(f"triage skipped: {exc}", file=sys.stderr)
    if failed_chunks or deferred:
        print(f"all chunks complete WITH FAILURES ({summary}) — supervisor must finish those tasks")
    else:
        print("all chunks complete")
    return 1 if (failed_chunks or deferred) else 0


if __name__ == "__main__":
    sys.exit(main())
