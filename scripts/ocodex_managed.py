#!/usr/bin/env python3
"""Management-layer entrypoint. doctor / run / status / wait — that is the whole UX.

  python3 ocodex_managed.py doctor
  python3 ocodex_managed.py run manifest.json --out-dir ./out
  python3 ocodex_managed.py run "Fix parser errors" --workdir . --owns src/parser.ts --out-dir ./out
  python3 ocodex_managed.py status ./out
  python3 ocodex_managed.py wait --out-dir ./out
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from harness_lib import (
    ManifestError,
    check_searxng,
    collect_status,
    estimate_requests,
    format_headroom,
    format_status_table,
    generate_manifest,
    load_manifest,
    plan_capacity,
    searxng_next_command,
    searxng_url,
    supervisor_brief,
    validate_manifest,
    write_json,
)

SKILL_DIR = Path(__file__).resolve().parent
REPO_DIR = SKILL_DIR.parent
LAUNCHER = SKILL_DIR / "launch_batches.py"
WAITER = SKILL_DIR / "wait_done.py"


def run(cmd: list[str]) -> int:
    return subprocess.call(cmd)


def find_ocodex() -> str | None:
    candidates = [
        os.environ.get("OCODEX_BIN"),
        shutil.which("ocodex"),
        str(Path.home() / "bin" / "ocodex"),
        str(Path.home() / ".local" / "bin" / "ocodex"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def find_orslot() -> str | None:
    candidates = [
        os.environ.get("ORSLOT_BIN"),
        shutil.which("orslot"),
        str(Path.home() / "bin" / "orslot"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def probe_pool() -> tuple[int, int]:
    import importlib.util
    spec = importlib.util.spec_from_file_location("launch_batches", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.slot_pool()


def resolve_target(target: str, args) -> tuple[Path, dict]:
    path = Path(target)
    if path.suffix == ".json" and path.is_file():
        return path, load_manifest(path)
    owns = list(args.owns or [])
    if not args.workdir:
        raise ManifestError(
            "goal-mode requires --workdir (or pass a manifest .json). "
            "Example: ocodex_managed.py run 'Fix the parser' --workdir . --owns src/parser.ts"
        )
    data = generate_manifest(
        target,
        args.workdir,
        owns=owns,
        name=args.name,
        mode=args.mode,
        facts=args.facts,
        effort=args.effort,
    )
    return path, data


def print_and_save_brief(out: Path, workdir, agents, args) -> str:
    brief = supervisor_brief(
        out=out,
        workdir=workdir,
        agents=agents,
        skill_dir=REPO_DIR,
        verify=args.verify,
        facts=args.facts if isinstance(args.facts, str) else (
            "; ".join(args.facts) if args.facts else None
        ),
        off_limits=args.off_limits,
    )
    print(brief)
    out.mkdir(parents=True, exist_ok=True)
    (out / "supervisor-brief.md").write_text(brief + "\n", encoding="utf-8")
    print(f"(also written to {out}/supervisor-brief.md)")
    return brief


def cmd_doctor() -> int:
    print("=== ocodex doctor ===")
    status = 0
    print(f"OK    python3   {sys.executable} ({sys.version.split()[0]})")

    ocodex = find_ocodex()
    if ocodex:
        print(f"OK    ocodex     {ocodex}")
    else:
        status = 1
        print("MISS  ocodex     not on PATH. Install it or set OCODEX_BIN.")
        print("      workers cannot launch until this exists.")

    orslot = find_orslot()
    if orslot:
        print(f"OK    orslot     {orslot}  (multi-key pool)")
    else:
        print("OPT   orslot     not found — 1 key, default 6 concurrent. Scale with `orslot add`.")

    docker = shutil.which("docker")
    if docker:
        print(f"OK    docker     {docker}")
    else:
        status = 1
        print("MISS  docker     not on PATH. SearXNG needs Docker.")
        print("      Install Docker, then: docker compose -f examples/searxng-compose.yml up -d")

    ok, url, detail = check_searxng()
    if ok:
        print(f"OK    searxng    {url}  ({detail})")
    else:
        status = 1
        print(f"MISS  searxng    not reachable at {url}  ({detail})")
        print("      Workers cannot web-search without SearXNG. Next:")
        for line in searxng_next_command().splitlines():
            print(f"      {line}")
        print(f"      Current SEARXNG_URL={searxng_url()}")

    skill = Path.home() / ".claude/skills/ocodex"
    if (skill / "SKILL.md").exists() and (skill / "scripts/launch_batches.py").exists():
        print(f"OK    skill      {skill}")
    elif (REPO_DIR / "SKILL.md").exists():
        print(f"OK    skill      {REPO_DIR}  (repo checkout; run ./install.sh to copy into ~/.claude/skills)")
    else:
        status = 1
        print(f"MISS  skill      {skill} — run ./install.sh from the repo")

    sample = REPO_DIR / "examples" / "sample-manifest.json"
    if not sample.exists():
        sample = skill / "examples" / "sample-manifest.json"
    if sample.exists():
        print(f"OK    sample     {sample}")
    else:
        print("MISS  sample     examples/sample-manifest.json")

    print()
    print("default ~6 concurrent workers per OpenRouter key (override --workers-per-key 8, not 20).")
    print("one supervisor is the quality gate. 429s backoff/hop; they are not a crash at 7+.")
    print()
    print("Next:")
    dest = skill if (skill / "scripts/ocodex_managed.py").exists() else SKILL_DIR
    sample_path = sample if sample.exists() else "<manifest.json>"
    print(f"  python3 {dest}/ocodex_managed.py run {sample_path} --out-dir /tmp/ocodex-sample")
    print(f"  python3 {dest}/ocodex_managed.py status /tmp/ocodex-sample")
    print("  then spawn one supervisor with the printed SUPERVISOR BRIEF")
    return status


def cmd_status(args) -> int:
    out = Path(args.out_dir).expanduser()
    if not out.exists():
        print(f"out-dir does not exist: {out}", file=sys.stderr)
        return 2
    interval = max(0.2, float(args.watch_interval)) if args.watch else 0
    while True:
        rows = collect_status(out)
        table = format_status_table(rows, out=out)
        if args.watch:
            print("\033[H\033[J", end="")
        print(table)
        all_done = out / "all.done"
        if args.watch:
            if all_done.exists() and not args.until_done_never:
                print()
                print(f"all.done: {all_done.read_text(encoding='utf-8').strip()}")
                return 0 if all_done.read_text(encoding="utf-8").strip() == "done" else 1
            time.sleep(interval)
            continue
        if any(row["state"] == "STALE" for row in rows):
            return 0  # informational; supervisor decides
        return 0


def cmd_run(args) -> int:
    try:
        source, data = resolve_target(args.target, args)
        workdir, agents = validate_manifest(
            data, args.timeout or 900, max_agents=None, require_workdir=not args.dry_run,
        )
    except ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    out = Path(args.out_dir).expanduser()
    keys, remaining = probe_pool()
    plan = plan_capacity(
        len(agents), keys, remaining,
        workers_per_key=args.workers_per_key or 6,
        max_workers=args.max_workers or 0,
    )
    print(format_headroom(plan, estimate_requests(agents)))
    print()

    out.mkdir(parents=True, exist_ok=True)
    man_path = source if source.suffix == ".json" and source.is_file() else (out / "manifest.json")
    if not (source.suffix == ".json" and source.is_file()):
        data["workdir"] = str(workdir)
        write_json(man_path, data)
        print(f"wrote generated manifest -> {man_path}")

    print_and_save_brief(out, workdir, agents, args)
    print()
    print("Spawn the supervisor now with that brief (workers follow in this process).")
    print("Watch the slot board: python3 scripts/ocodex_managed.py status", out)
    if args.dry_run:
        print("[dry-run] not launching workers.")
        return 0

    cmd = [sys.executable, str(LAUNCHER), str(man_path), "--out-dir", str(out)]
    if args.workers_per_key is not None:
        cmd.extend(["--workers-per-key", str(args.workers_per_key)])
    if args.max_workers is not None:
        cmd.extend(["--max-workers", str(args.max_workers)])
    if args.timeout is not None:
        cmd.extend(["--timeout", str(args.timeout)])

    if args.watch_status:
        proc = subprocess.Popen(cmd)
        while proc.poll() is None:
            print("\n" + format_status_table(collect_status(out), out=out))
            time.sleep(max(0.5, float(args.watch_interval)))
        code = proc.returncode or 0
    else:
        code = run(cmd)

    wait_code = 0
    if not args.no_wait:
        wait_code = run([
            sys.executable, str(WAITER), str(out),
            "--timeout", str(args.wait_timeout), "--poll", "0.5",
        ])
    print()
    print(format_status_table(collect_status(out), out=out))
    print(f"workers finished (launch={code} wait={wait_code}). Supervisor still required.")
    print(f"brief: {out}/supervisor-brief.md")
    return code if code != 0 else wait_code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    runp = sub.add_parser("run", help="validate, ledger, launch waves, wait, print supervisor brief")
    runp.add_argument("target", help="manifest.json OR a short goal string")
    runp.add_argument("--out-dir", required=True)
    runp.add_argument("--workdir", help="required when target is a goal, not a json file")
    runp.add_argument("--owns", action="append", default=[], help="owned path (repeatable). implies worker")
    runp.add_argument("--name", help="agent name for goal-mode")
    runp.add_argument("--mode", choices=["scout", "worker"])
    runp.add_argument("--effort", choices=["low", "medium", "high", "xhigh"])
    runp.add_argument("--facts", help="authoritative facts for workers + supervisor brief")
    runp.add_argument("--verify", help="real build/test commands for the supervisor brief")
    runp.add_argument("--off-limits", help="concurrent files the supervisor must not touch")
    runp.add_argument("--workers-per-key", type=int, help="default 6; 8 is ok; do not default to 20")
    runp.add_argument("--max-workers", type=int)
    runp.add_argument("--timeout", type=int)
    runp.add_argument("--no-wait", action="store_true")
    runp.add_argument("--wait-timeout", type=float, default=1500)
    runp.add_argument("--dry-run", action="store_true", help="print headroom + brief; do not launch")
    runp.add_argument("--watch-status", action="store_true", help="print the slot board while launching")
    runp.add_argument("--watch-interval", type=float, default=5)

    launch = sub.add_parser("launch", help="lower-level: launch_batches then wait_done")
    launch.add_argument("manifest")
    launch.add_argument("--out-dir", required=True)
    launch.add_argument("--workers-per-key", type=int)
    launch.add_argument("--max-workers", type=int)
    launch.add_argument("--timeout", type=int)
    launch.add_argument("--no-wait", action="store_true")
    launch.add_argument("--wait-timeout", type=float, default=1500)

    wait = sub.add_parser("wait", help="block until all.done")
    wait.add_argument("--out-dir", required=True)
    wait.add_argument("--timeout", type=float, default=1500)
    wait.add_argument("--poll", type=float, default=2)

    st = sub.add_parser("status", help="live slot board (no LLM)")
    st.add_argument("out_dir", nargs="?", help="batch out-dir")
    st.add_argument("--out-dir", dest="out_dir_flag")
    st.add_argument("--watch", action="store_true")
    st.add_argument("--watch-interval", type=float, default=5)
    st.add_argument("--until-done-never", action="store_true", help=argparse.SUPPRESS)

    sub.add_parser("doctor", help="check ocodex / orslot / docker / SearXNG / skill")

    args = ap.parse_args()
    if args.cmd == "doctor":
        return cmd_doctor()
    if args.cmd == "status":
        args.out_dir = args.out_dir_flag or args.out_dir
        if not args.out_dir:
            print("usage: ocodex_managed.py status <out-dir> [--watch]", file=sys.stderr)
            return 2
        return cmd_status(args)
    if args.cmd == "wait":
        return run([
            sys.executable, str(WAITER), args.out_dir,
            "--timeout", str(args.timeout), "--poll", str(args.poll),
        ])
    if args.cmd == "run":
        return cmd_run(args)

    cmd = [sys.executable, str(LAUNCHER), args.manifest, "--out-dir", args.out_dir]
    if args.workers_per_key is not None:
        cmd.extend(["--workers-per-key", str(args.workers_per_key)])
    if args.max_workers is not None:
        cmd.extend(["--max-workers", str(args.max_workers)])
    if args.timeout is not None:
        cmd.extend(["--timeout", str(args.timeout)])
    code = run(cmd)
    if args.no_wait:
        return code
    wait_code = run([
        sys.executable, str(WAITER), args.out_dir,
        "--timeout", str(args.wait_timeout), "--poll", "0.5",
    ])
    return code if code != 0 else wait_code


if __name__ == "__main__":
    raise SystemExit(main())
