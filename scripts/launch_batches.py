#!/usr/bin/env python3
"""launch_batches.py — capacity-aware multi-batch ocodex launcher.

Takes ONE master manifest with any number of agents and:
  1. probes the orslot pool (keys x remaining budget) and caps concurrency
     at slots * WORKERS_PER_KEY (default 5);
  2. injects a CHECKPOINT footer into every task so a crashed worker leaves
     its partial progress behind in <out>/<name>.progress.md;
  3. chunks agents into sub-batches (<=5 each, ownership kept intact —
     agents are never split from their files), launching them through the
     shared runner in waves that respect the concurrency cap;
  4. writes <out>/chunk-N.done markers and a final <out>/all.done, so a
     supervisor watches exactly one file instead of polling folder listings.

Usage:
  python3 launch_batches.py master-manifest.json --out-dir /path/out
                            [--workers-per-key 5] [--max-workers N]
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import os

SKILL_DIR = Path(__file__).resolve().parent
RUNNER = Path(os.environ.get("OCODEX_RUNNER", "")) if os.environ.get("OCODEX_RUNNER") else (
    SKILL_DIR / "run_agents.py"
    if (SKILL_DIR / "run_agents.py").exists()
    else Path.home() / ".claude/skills/ocodex-subagents/scripts/run_agents.py"
)
ORSLOT = Path(os.environ.get("ORSLOT_BIN", str(Path.home() / "bin/orslot")))
RUNNER_BATCH_CAP = 5          # agents per runner invocation
DEFAULT_WORKERS_PER_KEY = 5


def slot_pool() -> tuple[int, int]:
    """(number of keys, requests remaining today across the pool)."""
    if not ORSLOT.exists():
        # No slot manager: assume one key; the provider enforces its own caps.
        return 1, 10**9
    try:
        out = subprocess.run([str(ORSLOT)], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 1, 1000
    keys = re.findall(r"^\s*\*?\s*\d+\s+.*?(\d+)/(\d+)\s", out, re.MULTILINE)
    if not keys:
        return 1, 1000
    remaining = sum(int(cap) - int(used) for used, cap in keys)
    return len(keys), max(0, remaining)


CHECKPOINT = (
    "\n\nCHECKPOINT (mandatory): create {out}/{name}.progress.md IMMEDIATELY "
    "and append to it after every significant step — findings so far, files "
    "changed, next intended step. If your process dies, that file is the only "
    "thing that survives; a supervisor will finish your task from it."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--workers-per-key", type=int, default=DEFAULT_WORKERS_PER_KEY)
    ap.add_argument("--max-workers", type=int, default=0)
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    master = json.loads(Path(args.manifest).read_text())
    agents = master["agents"]

    keys, remaining = slot_pool()
    cap = keys * args.workers_per_key
    if args.max_workers:
        cap = min(cap, args.max_workers)
    # ~40 requests is a sane per-agent budget guess; refuse to strand a batch
    est_need = len(agents) * 40
    print(f"pool: {keys} keys, ~{remaining} requests left today; "
          f"concurrency cap {cap}; {len(agents)} agents (~{est_need} reqs est.)")
    if remaining < est_need:
        print("WARNING: pool may run dry mid-batch — consider fewer agents "
              "or `orslot add` more keys.", file=sys.stderr)

    for agent in agents:
        agent["task"] += CHECKPOINT.format(out=out, name=agent["name"])

    # Chunks must fit under the concurrency cap or the wave loop deadlocks
    # waiting for room that can never exist.
    chunk_size = max(1, min(RUNNER_BATCH_CAP, cap))
    chunks = [agents[i:i + chunk_size] for i in range(0, len(agents), chunk_size)]
    ledger = {"chunks": [], "out": str(out)}
    running: list[tuple[int, subprocess.Popen]] = []
    launched = 0

    def live_workers() -> int:
        return sum(len(chunks[i]) for i, p in running if p.poll() is None)

    while launched < len(chunks) or running:
        # reap
        for i, p in running[:]:
            if p.poll() is not None:
                (out / f"chunk-{i}.done").write_text(str(p.returncode))
                running.remove((i, p))
                print(f"chunk-{i} finished (exit {p.returncode})")
        # launch within cap
        while launched < len(chunks) and live_workers() + len(chunks[launched]) <= cap:
            i = launched
            sub = copy.deepcopy(master)
            sub["agents"] = chunks[i]
            mpath = out / f"chunk-{i}.manifest.json"
            mpath.write_text(json.dumps(sub, indent=2))
            log = open(out / f"chunk-{i}.runner.log", "w")
            p = subprocess.Popen(
                [sys.executable, str(RUNNER), str(mpath), "--out-dir", str(out / f"chunk-{i}")],
                stdout=log, stderr=subprocess.STDOUT,
            )
            running.append((i, p))
            ledger["chunks"].append({"chunk": i, "agents": [a["name"] for a in chunks[i]]})
            (out / "ledger.json").write_text(json.dumps(ledger, indent=2))
            print(f"chunk-{i} launched ({len(chunks[i])} agents)")
            launched += 1
        time.sleep(5)

    (out / "all.done").write_text("done")
    print("all chunks complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
