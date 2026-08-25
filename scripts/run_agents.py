#!/usr/bin/env python3
"""Run a bounded batch of external ocodex workers from a JSON manifest."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify import classify  # noqa: E402


MAX_AGENTS = 6
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def fail(message: str) -> None:
    raise ValueError(message)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read manifest: {exc}")
    if not isinstance(data, dict):
        fail("manifest must be a JSON object")
    return data


def validate(data: dict[str, Any], default_timeout: int) -> tuple[Path, list[dict[str, Any]]]:
    raw_workdir = data.get("workdir")
    if not isinstance(raw_workdir, str) or not raw_workdir:
        fail("workdir must be a non-empty absolute path")
    workdir = Path(raw_workdir).expanduser()
    if not workdir.is_absolute() or not workdir.is_dir():
        fail(f"workdir is not an existing absolute directory: {workdir}")

    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        fail("agents must be a non-empty list")
    if len(agents) > MAX_AGENTS:
        fail(f"at most {MAX_AGENTS} agents may run in one batch")

    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    owned_paths: list[Path] = []
    for index, raw in enumerate(agents):
        if not isinstance(raw, dict):
            fail(f"agent {index} must be an object")
        name = raw.get("name")
        task = raw.get("task")
        mode = raw.get("mode", "scout")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            fail(f"agent {index} has an invalid name")
        if name in names:
            fail(f"duplicate agent name: {name}")
        names.add(name)
        if not isinstance(task, str) or not task.strip():
            fail(f"agent {name} needs a non-empty task")
        if mode not in {"scout", "worker"}:
            fail(f"agent {name} mode must be scout or worker")

        owns = raw.get("owns", [])
        if not isinstance(owns, list) or not all(isinstance(item, str) and item for item in owns):
            fail(f"agent {name} owns must be a list of paths")
        if mode == "worker" and not owns:
            fail(f"worker {name} needs at least one owned path")
        for item in owns:
            owned_path = (workdir / item).resolve() if not Path(item).is_absolute() else Path(item).resolve()
            if not owned_path.is_relative_to(workdir.resolve()):
                fail(f"worker ownership must stay inside workdir: {item}")
            if any(
                owned_path == previous
                or owned_path.is_relative_to(previous)
                or previous.is_relative_to(owned_path)
                for previous in owned_paths
            ):
                fail(f"worker ownership overlaps at {item}")
            owned_paths.append(owned_path)

        timeout = raw.get("timeout", default_timeout)
        if not isinstance(timeout, int) or timeout < 30:
            fail(f"agent {name} timeout must be an integer of at least 30 seconds")
        model = raw.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            fail(f"agent {name} model must be a non-empty string")
        effort = raw.get("effort")
        if effort is not None and effort not in {"low", "medium", "high", "xhigh"}:
            fail(f"agent {name} effort must be low, medium, high, or xhigh")
        after = raw.get("after", [])
        if after is None:
            after = []
        if not isinstance(after, list) or not all(isinstance(item, str) and NAME_RE.fullmatch(item) for item in after):
            fail(f"agent {name} after must be a list of agent names")
        kind = raw.get("kind")
        if kind is not None and (not isinstance(kind, str) or not kind.strip()):
            fail(f"agent {name} kind must be a non-empty string")
        risk = raw.get("risk")
        if risk is not None and risk not in {"low", "medium", "high"}:
            fail(f"agent {name} risk must be low, medium, or high")
        stall = raw.get("stall")
        if stall is not None and (not isinstance(stall, int) or stall < 0):
            fail(f"agent {name} stall must be an integer number of seconds (0 disables)")
        normalized.append({
            "name": name,
            "task": task.strip(),
            "mode": mode,
            "owns": owns,
            "timeout": timeout,
            "model": model,
            "effort": effort,
            "after": after,
            "kind": kind.strip() if isinstance(kind, str) else None,
            "risk": risk,
            "stall": stall,
        })
    return workdir.resolve(), normalized


def build_prompt(agent: dict[str, Any], workdir: Path) -> str:
    common = (
        "You are an external ocodex worker reporting to a parent agent. "
        "You have no access to the parent conversation, so use only this task and local evidence. "
        "Do not spawn subagents or run codex/ocodex commands. Preserve unrelated user changes. "
        "Do not mutate external systems unless the task explicitly requests and authorizes it. "
        "For ordinary read-only shell calls, omit justification and sandbox_permissions. "
        "Return a concise result with evidence, files inspected or changed, tests run, and any uncertainty."
    )
    if agent["mode"] == "scout":
        boundary = "This is read-only. Do not edit files or mutate external systems."
    else:
        owned = ", ".join(agent["owns"])
        boundary = (
            f"You own only these paths: {owned}. Other workers may be editing elsewhere. "
            "Do not edit outside your ownership or revert others' work."
        )
    return f"{common}\n\nWorking directory: {workdir}\n{boundary}\n\nTask:\n{agent['task']}"


def find_ocodex() -> str | None:
    candidates = [
        os.environ.get("OCODEX_BIN"),
        shutil.which("ocodex"),
        str(Path.home() / "bin" / "ocodex"),
        str(Path.home() / ".local" / "bin" / "ocodex"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def subprocess_environment() -> dict[str, str]:
    env = os.environ.copy()
    additions = [
        str(Path.home() / "bin"),
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".hermes" / "node" / "bin"),
        str(Path.home() / ".bun" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    current = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(dict.fromkeys([*additions, *current.split(os.pathsep)]))
    return env


def command_for(
    agent: dict[str, Any],
    workdir: Path,
    final_file: Path,
    default_model: str | None,
    ocodex_bin: str,
) -> list[str]:
    command = [
        ocodex_bin,
        "-a",
        "never",
        "-s",
        "read-only" if agent["mode"] == "scout" else "workspace-write",
    ]
    model = agent["model"] or default_model
    if model:
        command.extend(["-m", model])
    effort = agent["effort"] or ("low" if agent["mode"] == "scout" else None)
    if effort:
        command.extend(["-c", f'model_reasoning_effort="{effort}"'])
    command.extend([
        "--disable",
        "multi_agent",
        "-c",
        "agents.enabled=false",
        "exec",
        "-C",
        str(workdir),
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "-o",
        str(final_file),
        "-",
    ])
    return command


def _tail_lines(path: Path, n: int = 8) -> list[str]:
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    except OSError:
        return []
    return lines[-n:]


def write_live(out_dir: Path, payload: dict[str, Any]) -> None:
    try:
        (out_dir / "live.json").write_text(json.dumps(payload, indent=2) + "\n")
    except OSError:
        pass


def checkpoint_paths(out_dir: Path, name: str) -> list[Path]:
    return [
        out_dir / f"{name}.progress.md",
        out_dir.parent / f"{name}.progress.md",
    ]


def stall_age(out_dir: Path, name: str, started: float, now: float) -> float:
    """Seconds since the last checkpoint write; missing file counts as 'since start'."""
    newest: float | None = None
    for path in checkpoint_paths(out_dir, name):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return now - (newest if newest is not None else started)


def terminate(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_one(
    agent: dict[str, Any],
    workdir: Path,
    out_dir: Path,
    default_model: str | None,
    ocodex_bin: str,
) -> dict[str, Any]:
    name = agent["name"]
    final_file = out_dir / f"{name}.final.txt"
    stdout_file = out_dir / f"{name}.stdout.log"
    stderr_file = out_dir / f"{name}.stderr.log"
    final_file.unlink(missing_ok=True)
    command = command_for(agent, workdir, final_file, default_model, ocodex_bin)
    started = time.monotonic()
    started_wall = time.time()
    with stdout_file.open("w", encoding="utf-8") as stdout, stderr_file.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
            env=subprocess_environment(),
        )
        assert process.stdin is not None
        try:
            process.stdin.write(build_prompt(agent, workdir))
        except BrokenPipeError:
            pass
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        deadline = started + agent["timeout"]
        stall_limit = agent.get("stall")
        if stall_limit is None:
            stall_limit = int(os.environ.get("OCODEX_STALL_S", "600"))
        final_signature: tuple[int, int] | None = None
        final_stable_since: float | None = None
        timed_out = False
        stalled = False
        completed_from_final = False
        last_live = 0.0
        while True:
            return_code = process.poll()
            if return_code is not None:
                break
            now = time.monotonic()
            if now - last_live >= 2:
                write_live(out_dir, {
                    "name": name,
                    "state": "running",
                    "pid": process.pid,
                    "seconds": round(now - started, 1),
                    "stdout_tail": _tail_lines(stdout_file),
                    "stderr_tail": _tail_lines(stderr_file),
                })
                last_live = now
            if final_file.exists() and final_file.stat().st_size:
                stat = final_file.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                if signature != final_signature:
                    final_signature = signature
                    final_stable_since = now
                elif final_stable_since is not None and now - final_stable_since >= 2:
                    # This local ocodex can finish `-o` but linger in shutdown hooks.
                    # The completed final file is the authoritative worker result.
                    terminate(process)
                    return_code = 0
                    completed_from_final = True
                    break
            if stall_limit and stall_age(out_dir, name, started_wall, time.time()) >= stall_limit:
                terminate(process)
                return_code = process.returncode if process.returncode is not None else 124
                stalled = True
                timed_out = True
                break
            if now >= deadline:
                terminate(process)
                return_code = process.returncode if process.returncode is not None else 124
                timed_out = True
                break
            time.sleep(0.2)
    final_ok = final_file.exists() and bool(final_file.read_text(encoding="utf-8").strip())
    result = {
        "name": name,
        "mode": agent["mode"],
        "ok": return_code == 0 and final_ok and not timed_out,
        "return_code": return_code,
        "timed_out": timed_out,
        "stalled": stalled,
        "terminated_after_final": completed_from_final,
        "seconds": round(time.monotonic() - started, 2),
        "final_file": str(final_file),
        "stdout_file": str(stdout_file),
        "stderr_file": str(stderr_file),
        "kind": agent.get("kind"),
        "risk": agent.get("risk"),
    }
    if not result["ok"]:
        harmless = "/Users/michael/bin/ocodex:25: device not configured: /dev/tty"
        stderr_lines = [
            line for line in stderr_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and line.strip() != harmless
        ]
        result["error_tail"] = stderr_lines[-12:]
    result["class"] = classify(result)
    write_live(out_dir, {
        "name": name,
        "state": "ok" if result["ok"] else "failed",
        "seconds": result["seconds"],
        "class": result["class"],
        "stdout_tail": _tail_lines(stdout_file),
        "stderr_tail": _tail_lines(stderr_file),
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--max-parallel", type=int, default=6)
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ocodex_bin = find_ocodex()
    if ocodex_bin is None:
        print("ocodex executable not found; set OCODEX_BIN or install it in ~/bin", file=sys.stderr)
        return 2
    if not 1 <= args.max_parallel <= MAX_AGENTS:
        print(f"--max-parallel must be between 1 and {MAX_AGENTS}", file=sys.stderr)
        return 2
    if args.timeout < 30:
        print("--timeout must be at least 30 seconds", file=sys.stderr)
        return 2

    try:
        workdir, agents = validate(load_manifest(args.manifest), args.timeout)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.out_dir:
        out_root = args.out_dir.expanduser().resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        out_dir = Path(tempfile.mkdtemp(prefix="batch-", dir=out_root))
    else:
        out_dir = Path(tempfile.mkdtemp(prefix="ocodex-batch-"))

    if args.dry_run:
        plan = []
        for agent in agents:
            final_file = out_dir / f"{agent['name']}.final.txt"
            plan.append({
                "name": agent["name"],
                "mode": agent["mode"],
                "command": command_for(agent, workdir, final_file, args.model, ocodex_bin),
            })
        print(json.dumps({"out_dir": str(out_dir), "dry_run": True, "agents": plan}, indent=2))
        return 0

    worker_count = min(args.max_parallel, len(agents))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(run_one, agent, workdir, out_dir, args.model, ocodex_bin) for agent in agents]
        results = [future.result() for future in futures]

    summary = {"out_dir": str(out_dir), "ok": all(item["ok"] for item in results), "agents": results}
    (out_dir / "results.json").write_text(json.dumps(summary, indent=2))
    if args.out_dir:
        (out_root / "results.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
