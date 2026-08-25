#!/usr/bin/env python3
"""launch_batches.py — capacity-aware pool launcher for ocodex workers.

Takes ONE master manifest with any number of agents and:
  1. probes the orslot pool (keys x remaining budget) and caps concurrency
     at slots * WORKERS_PER_KEY (default 5);
  2. injects a structured CHECKPOINT into every task so a crashed worker
     leaves <out>/<name>.progress.md;
  3. runs a work-stealing pool: one runner process per agent, the next job
     starts the moment a slot frees (no wave boundaries);
  4. reassigns a dead agent ONCE from its checkpoint (skipped when the
     failure is classified fatal); a second death is left to the supervisor;
  5. honors optional per-agent "after": ["other-name"] so scouts can finish
     before fixers spawn, without a second launch;
  6. writes <out>/status.json + status.txt every poll, plus all.done.

Usage:
  python3 launch_batches.py master-manifest.json --out-dir /path/out
                            [--workers-per-key 5] [--max-workers N]
                            [--context-pack] [--watch / --no-watch]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILL_DIR))

from classify import classify, should_retry  # noqa: E402
from fleet_state import collect_state, render_table, write_status  # noqa: E402

RUNNER = Path(os.environ["OCODEX_RUNNER"]).expanduser() if os.environ.get("OCODEX_RUNNER") else (
    SKILL_DIR / "run_agents.py"
)
ORSLOT = Path(os.environ.get("ORSLOT_BIN", str(Path.home() / "bin/orslot")))
STATS = Path(os.environ.get("OCODEX_STATS", str(Path.home() / ".ocodex" / "stats.jsonl")))
DEFAULT_WORKERS_PER_KEY = 5
POLL_INTERVAL = float(os.environ.get("OCODEX_POOL_POLL", "1"))


def slot_pool() -> tuple[int, int]:
    """(number of keys, requests remaining today across the pool)."""
    if not ORSLOT.exists():
        return 1, 10**9
    try:
        out = subprocess.run([str(ORSLOT)], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 1, 1000
    keys = re.findall(r"^\s*\*?\s*\d+\s+.*?(\d+)/(\d+)(?:\s|$)", out, re.MULTILINE)
    if not keys:
        return 1, 1000
    remaining = sum(int(cap) - int(used) for used, cap in keys)
    return len(keys), max(0, remaining)


CHECKPOINT = (
    "\n\nCHECKPOINT (mandatory): create {out}/{name}.progress.md IMMEDIATELY, "
    "before editing any owned file, using this exact shape:\n\n"
    "## Done\n- [ ] step\n\n"
    "## Files touched\n\n"
    "## Next step\n(one line: what you are doing RIGHT NOW)\n\n"
    "## Open questions\n\n"
    "One checkbox per completed sub-task, appended as you go — not one dump "
    "per session. Rewrite ## Next step whenever you start a new action so a "
    "watcher can see it. Append at least every 5 minutes even if still on the "
    "same step (prefix `still: `). If your process dies, that file is the only "
    "thing that survives."
)


class Job:
    def __init__(self, agent: dict, index: int):
        self.agent = agent
        self.index = index
        self.attempt = 0
        self.process: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.log = None
        self.last_class: str | None = None

    @property
    def name(self) -> str:
        return self.agent["name"]

    @property
    def deps(self) -> list[str]:
        raw = self.agent.get("after") or []
        return list(raw) if isinstance(raw, list) else []

    def tag(self) -> str:
        return f"{self.name}-retry" if self.attempt else self.name


def load_context(workdir: Path, out: Path, rebuild: bool) -> str:
    """Return a context pack to prepend, or empty string."""
    cached = workdir / "CONTEXT.md"
    dest = out / "CONTEXT.md"
    if rebuild:
        packer = SKILL_DIR / "contextpack.py"
        if packer.is_file():
            subprocess.run(
                [sys.executable, str(packer), str(workdir), "--out", str(dest)],
                check=False,
            )
        if dest.exists():
            return dest.read_text(encoding="utf-8", errors="replace")[:12000]
    if cached.exists():
        text = cached.read_text(encoding="utf-8", errors="replace")[:12000]
        dest.write_text(text)
        return text
    return ""


def build_manifest(job: Job, master: dict, out: Path, context: str) -> dict:
    sub = {"workdir": master["workdir"], "agents": [dict(job.agent)]}
    agent = sub["agents"][0]
    task = agent["task"]
    if context:
        task = (
            "CONTEXT PACK (the working tree is truth if they disagree):\n\n"
            + context.strip()
            + "\n\n---\n\n"
            + task
        )
    task += CHECKPOINT.format(out=out, name=job.name)
    if job.attempt == 1:
        progress_file = out / f"{job.name}.progress.md"
        extra = (
            "\n\nYOUR PREVIOUS PROCESS DIED MID-TASK. This is your ONE reassignment. "
            "Do not restart from scratch. Resume from the checkpoint below. "
            "Verify its done-steps against the working tree before trusting them.\n"
        )
        if progress_file.exists():
            extra += "\n---\n" + progress_file.read_text(encoding="utf-8")[-8000:] + "\n---\n"
        else:
            extra += "\nNo checkpoint file was found; finish the original task.\n"
        task += extra
    agent["task"] = task
    return sub


def spawn(job: Job, master: dict, out: Path, context: str) -> None:
    tag = job.tag()
    mpath = out / f"{tag}.manifest.json"
    mpath.write_text(json.dumps(build_manifest(job, master, out, context), indent=2))
    log = open(out / f"{tag}.runner.log", "w")
    job.log = log
    job.started_at = time.time()
    job.process = subprocess.Popen(
        [sys.executable, str(RUNNER), str(mpath), "--out-dir", str(out / tag)],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def kill_all(running: list[Job]) -> None:
    for job in running:
        p = job.process
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if job.log is not None:
            try:
                job.log.close()
            except Exception:
                pass


def append_stat(record: dict) -> None:
    try:
        STATS.parent.mkdir(parents=True, exist_ok=True)
        with STATS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def load_runner_result(out: Path, job: Job) -> dict:
    path = out / job.tag() / "results.json"
    if not path.exists():
        matches = list((out / job.tag()).glob("**/results.json")) if (out / job.tag()).is_dir() else []
        path = matches[0] if matches else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        agents = data.get("agents") or []
        if agents and isinstance(agents[0], dict):
            return agents[0]
    except (OSError, json.JSONDecodeError):
        pass
    return {"ok": False, "return_code": None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--workers-per-key", type=int, default=DEFAULT_WORKERS_PER_KEY)
    ap.add_argument("--max-workers", type=int, default=0)
    ap.add_argument("--context-pack", action="store_true",
                    help="build CONTEXT.md from the workdir and inject it into every task")
    ap.add_argument("--watch", dest="watch", action="store_true", default=None,
                    help="redraw a live table on a tty (default: on when stdout is a tty)")
    ap.add_argument("--no-watch", dest="watch", action="store_false")
    args = ap.parse_args()

    if not RUNNER.is_file():
        print(
            f"runner not found: {RUNNER} "
            "(keep run_agents.py beside this launcher, or set OCODEX_RUNNER)",
            file=sys.stderr,
        )
        return 2

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stale = [p.name for p in out.glob("*.done")]
    if stale:
        print(
            f"refusing to run: {', '.join(sorted(stale))} already exist in {out} "
            "(use a fresh out-dir or delete old markers)",
            file=sys.stderr,
        )
        return 2

    master = json.loads(Path(args.manifest).read_text())
    names = [agent["name"] for agent in master["agents"]]
    if len(names) != len(set(names)):
        print("duplicate agent names in manifest", file=sys.stderr)
        return 2
    name_set = set(names)
    for agent in master["agents"]:
        for dep in agent.get("after") or []:
            if dep not in name_set:
                print(f"agent {agent['name']} after=[{dep}] does not name another agent", file=sys.stderr)
                return 2
            if dep == agent["name"]:
                print(f"agent {agent['name']} cannot depend on itself", file=sys.stderr)
                return 2

    jobs = [Job(agent, i) for i, agent in enumerate(master["agents"])]
    workdir = Path(master["workdir"])
    context = load_context(workdir, out, rebuild=args.context_pack)

    keys, remaining = slot_pool()
    cap = keys * args.workers_per_key
    if args.max_workers:
        cap = min(cap, args.max_workers)
    cap = max(1, cap)
    est_need = len(jobs) * 40
    use_watch = sys.stdout.isatty() if args.watch is None else args.watch
    print(
        f"pool: {keys} keys, ~{remaining} requests left today; "
        f"concurrency cap {cap}; {len(jobs)} agents (~{est_need} reqs est.)"
    )
    print(f"status: {out}/status.txt")
    print(f"live:   python3 {SKILL_DIR}/fleet_watch.py {out}")
    if remaining < est_need:
        print(
            "WARNING: pool may run dry mid-batch — consider fewer agents "
            "or `orslot add` more keys.",
            file=sys.stderr,
        )

    pending: list[Job] = list(jobs)
    waiting: list[Job] = []
    running: list[Job] = []
    failed: list[str] = []
    blocked: list[str] = []
    succeeded: set[str] = set()
    batch_started = time.time()
    ledger: dict = {
        "jobs": [],
        "out": str(out),
        "cap": cap,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    events: deque[str] = deque(maxlen=12)

    def live() -> int:
        return sum(1 for j in running if j.process and j.process.poll() is None)

    def emit(msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        events.append(f"{stamp} {msg}")
        if not use_watch:
            print(msg)

    def dep_state(job: Job) -> str:
        for dep in job.deps:
            if dep in failed or dep in blocked:
                return "blocked"
            if dep not in succeeded:
                return "waiting"
        return "ready"

    def take_next() -> Job | None:
        if waiting:
            return waiting.pop(0)
        still: list[Job] = []
        picked: Job | None = None
        for job in pending:
            state = dep_state(job)
            if state == "blocked":
                blocked.append(job.name)
                failed.append(job.name)
                (out / f"{job.name}.failed").write_text("blocked:" + ",".join(job.deps))
                emit(f"[{job.name}] BLOCKED (dependency failed: {', '.join(job.deps)})")
            elif state == "ready" and picked is None:
                picked = job
            else:
                still.append(job)
        pending[:] = still
        return picked

    def job_state(job: Job) -> str:
        if job.name in blocked:
            return "blocked"
        if job.name in failed:
            return "failed"
        if job.name in succeeded:
            return "ok"
        if job in running:
            return "retrying" if job.attempt else "running"
        if job in waiting:
            return "retrying"
        if job.deps and dep_state(job) == "blocked":
            return "blocked"
        return "queued"

    def refresh_status() -> None:
        rows = []
        for job in jobs:
            rows.append({
                "name": job.name,
                "mode": job.agent.get("mode") or "scout",
                "task": job.agent.get("task") or "",
                "goal": " ".join((job.agent.get("task") or "").split())[:68],
                "state": job_state(job),
                "attempt": job.attempt + 1,
                "started_at": job.started_at,
                "after": job.deps,
                "tag": job.tag() if job.process or job.started_at else job.name,
            })
        snapshot = collect_state(
            out,
            rows,
            cap=cap,
            started_at=batch_started,
            events=list(events),
        )
        write_status(out, snapshot)
        if use_watch:
            sys.stdout.write("\033[2J\033[H" + render_table(snapshot))
            sys.stdout.flush()

    def on_signal(signum, frame) -> None:
        kill_all(running)
        sys.exit(f"launch_batches killed by signal {signum}; runners terminated")

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    refresh_status()
    while pending or waiting or running:
        for job in running[:]:
            p = job.process
            assert p is not None
            code = p.poll()
            if code is None:
                continue
            running.remove(job)
            if job.log is not None:
                job.log.close()
                job.log = None
            tag = job.tag()
            seconds = round(time.time() - (job.started_at or time.time()), 1)
            result = load_runner_result(out, job)
            result.setdefault("return_code", code)
            result.setdefault("ok", code == 0)
            kind = result.get("class") or classify(result)
            job.last_class = kind
            append_stat({
                "ts": datetime.now(timezone.utc).isoformat(),
                "name": job.name,
                "attempt": job.attempt + 1,
                "ok": code == 0,
                "return_code": code,
                "seconds": seconds,
                "effort": job.agent.get("effort"),
                "mode": job.agent.get("mode"),
                "kind": job.agent.get("kind"),
                "class": kind,
            })
            if code == 0:
                (out / f"{tag}.done").write_text(str(code))
                succeeded.add(job.name)
                emit(f"[{tag}] finished ok")
            elif should_retry(result, job.attempt):
                emit(f"[{tag}] FAILED ({kind}, exit {code}); one reassignment from checkpoint")
                job.attempt = 1
                waiting.append(job)
            else:
                (out / f"{tag}.failed").write_text(str(code))
                failed.append(job.name)
                why = "retry also failed" if job.attempt else f"fatal ({kind})"
                emit(f"[{tag}] {why} (exit {code}) — left to supervisor")
        while live() < cap:
            job = take_next()
            if job is None:
                break
            spawn(job, master, out, context)
            running.append(job)
            emit(f"[{job.tag()}] launched (attempt {job.attempt + 1})")
            ledger["jobs"].append({
                "agent": job.name,
                "attempt": job.attempt + 1,
                "manifest": f"{job.tag()}.manifest.json",
                "mode": job.agent.get("mode"),
                "after": job.deps,
            })
            (out / "ledger.json").write_text(json.dumps(ledger, indent=2))
        if not running and not waiting and pending:
            for job in pending:
                failed.append(job.name)
                (out / f"{job.name}.failed").write_text("deadlock")
                emit(f"[{job.name}] deadlock (unsatisfiable after=) — left to supervisor")
            pending.clear()
        refresh_status()
        time.sleep(POLL_INTERVAL)

    # Any leftover pending at drain is a cycle or a missed block.
    for job in pending:
        if job.name not in failed:
            failed.append(job.name)
            (out / f"{job.name}.failed").write_text("never-started")
            emit(f"[{job.name}] never started — left to supervisor")

    summary = "failed:" + ",".join(sorted(set(failed))) if failed else "done"
    (out / "all.done").write_text(summary)
    refresh_status()
    if failed:
        emit(f"pool complete WITH FAILURES ({summary}) — supervisor must finish those tasks")
        return 1
    emit("all agents complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
