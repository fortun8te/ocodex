#!/usr/bin/env python3
"""Run a bounded batch of external ocodex workers from a JSON manifest."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

from classify import should_retry
from claims import parse_claims
from harness_lib import (
    MAX_AGENTS,
    ManifestError,
    append_stats,
    classify_error,
    compact_brief,
    is_stale_heartbeat,
    load_manifest,
    harvest_learned,
    merge_result_json,
    normalize_learned,
    parse_codex_jsonl,
    seed_progress_files,
    stagger_sec,
    utc_now,
    validate_manifest,
    write_json,
)
from providers import model_candidates, pick_provider, provider_available


LIVE_PROCESSES: list[subprocess.Popen] = []


def find_ocodex() -> str | None:
    env_bin = os.environ.get("OCODEX_BIN")
    if env_bin:
        path = Path(env_bin).expanduser()
        if path.is_file():
            return str(path.resolve())
    candidates = [
        shutil.which("ocodex"),
        str(Path.home() / "bin" / "ocodex"),
        str(Path.home() / ".local" / "bin" / "ocodex"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def find_codex() -> str | None:
    candidates = [
        os.environ.get("CODEX_BIN"),
        shutil.which("codex"),
        str(Path.home() / "bin" / "codex"),
        str(Path.home() / ".local" / "bin" / "codex"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None


def resolve_worker_bin(provider: dict | None, ocodex_bin: str) -> tuple[str, dict[str, str], list[str]]:
    """Non-OpenRouter providers skip the orslot wrapper and talk to `codex` directly."""
    extra_env: dict[str, str] = {}
    extra_args: list[str] = []
    if not provider or provider.get("name", "openrouter") == "openrouter":
        return ocodex_bin, extra_env, extra_args
    if not provider_available(provider):
        return ocodex_bin, extra_env, extra_args
    codex = find_codex()
    if not codex:
        return ocodex_bin, extra_env, extra_args
    key = os.environ.get(str(provider.get("key_env") or ""), "").strip()
    if key:
        extra_env["OPENROUTER_API_KEY"] = key
        extra_env["OPENAI_API_KEY"] = key
    extra_args = [
        "--profile", "openrouter",
        "-c", f"model_providers.openrouter.base_url={provider['base_url']}",
    ]
    extra_env["OCODEX_KEY_SLOT"] = str(provider.get("name"))
    return codex, extra_env, extra_args


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


def session_path(out_dir: Path, name: str) -> Path:
    return batch_out_dir(out_dir) / f"{name}.session.json"


def load_session_id(out_dir: Path, name: str) -> str | None:
    path = session_path(out_dir, name)
    data = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
    if isinstance(data, dict) and isinstance(data.get("session_id"), str) and data["session_id"].strip():
        return data["session_id"].strip()
    return None


def command_for(
    agent: dict[str, Any],
    workdir: Path,
    final_file: Path,
    default_model: str | None,
    ocodex_bin: str,
    extra_args: list[str] | None = None,
    session_id: str | None = None,
) -> list[str]:
    argv0: list[str]
    if ocodex_bin.endswith(".py"):
        argv0 = [sys.executable, ocodex_bin]
    else:
        argv0 = [ocodex_bin]
    command = [
        *argv0,
        *(extra_args or []),
        "-a",
        "never",
        "-s",
        "read-only" if agent["mode"] == "scout" else "workspace-write",
    ]
    model = default_model if default_model is not None else agent.get("model")
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
    ])
    if session_id:
        command.extend(["resume", session_id])
    command.extend([
        "-C",
        str(workdir),
        "--skip-git-repo-check",
        "--json",
        "--color",
        "never",
        "-o",
        str(final_file),
        "-",
    ])
    return command


def terminate(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _register(process: subprocess.Popen) -> None:
    LIVE_PROCESSES.append(process)


def _unregister(process: subprocess.Popen) -> None:
    try:
        LIVE_PROCESSES.remove(process)
    except ValueError:
        pass


def kill_live_children() -> None:
    for process in list(LIVE_PROCESSES):
        if process.poll() is None:
            terminate(process)
        _unregister(process)


def batch_out_dir(fallback: Path) -> Path:
    raw = os.environ.get("OCODEX_BATCH_OUT")
    if raw:
        return Path(raw)
    return fallback


def build_prompt(
    agent: dict[str, Any],
    workdir: Path,
    out_dir: Path,
    *,
    resume: bool = False,
) -> str:
    checkpoint_out = batch_out_dir(out_dir)
    result_path = checkpoint_out / f"{agent['name']}.result.json"
    return compact_brief(
        agent,
        workdir,
        checkpoint_out=checkpoint_out,
        result_path=result_path,
        resume=resume,
    )


def _run_attempt(
    agent: dict[str, Any],
    workdir: Path,
    out_dir: Path,
    default_model: str | None,
    ocodex_bin: str,
    attempt: int,
    extra_env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    name = agent["name"]
    suffix = "" if attempt == 1 else f".retry{attempt}"
    final_file = out_dir / f"{name}.final.txt"
    stdout_file = out_dir / f"{name}.stdout{suffix}.log"
    stderr_file = out_dir / f"{name}.stderr{suffix}.log"
    if attempt == 1:
        final_file.unlink(missing_ok=True)
    else:
        # Retry writes a fresh final; keep the failed first attempt aside.
        if final_file.exists():
            final_file.replace(out_dir / f"{name}.final.attempt1.txt")
        final_file.unlink(missing_ok=True)
    command = command_for(
        agent, workdir, final_file, default_model, ocodex_bin,
        extra_args=extra_args, session_id=session_id,
    )
    started = time.monotonic()
    env = subprocess_environment()
    if extra_env:
        env.update(extra_env)
    with stdout_file.open("w", encoding="utf-8") as stdout, stderr_file.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
            env=env,
        )
        _register(process)
        assert process.stdin is not None
        try:
            process.stdin.write(build_prompt(agent, workdir, out_dir, resume=attempt > 1))
        except BrokenPipeError:
            pass
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        deadline = started + agent["timeout"]
        final_signature: tuple[int, int] | None = None
        final_stable_since: float | None = None
        timed_out = False
        stale_heartbeat = False
        completed_from_final = False
        return_code: int | None = None
        progress_path = batch_out_dir(out_dir) / f"{name}.progress.md"
        try:
            while True:
                return_code = process.poll()
                if return_code is not None:
                    break
                now = time.monotonic()
                if final_file.exists() and final_file.stat().st_size:
                    stat = final_file.stat()
                    signature = (stat.st_size, stat.st_mtime_ns)
                    if signature != final_signature:
                        final_signature = signature
                        final_stable_since = now
                    elif final_stable_since is not None and now - final_stable_since >= 2:
                        # This local ocodex can finish `-o` but linger in shutdown hooks.
                        terminate(process)
                        return_code = 0
                        completed_from_final = True
                        break
                if now >= deadline:
                    terminate(process)
                    return_code = process.returncode if process.returncode is not None else 124
                    timed_out = True
                    break
                if is_stale_heartbeat(
                    progress_path,
                    attempt_started=started,
                    now=now,
                    attempt=attempt,
                ):
                    terminate(process)
                    return_code = process.returncode if process.returncode is not None else 124
                    stale_heartbeat = True
                    break
                time.sleep(0.2)
        finally:
            _unregister(process)
    final_ok = final_file.exists() and bool(final_file.read_text(encoding="utf-8").strip())
    stderr_text = ""
    if stderr_file.exists():
        stderr_text = stderr_file.read_text(encoding="utf-8")
    error_class = classify_error(
        return_code, timed_out, final_ok, stderr_text, stale_heartbeat=stale_heartbeat,
    )
    result = {
        "name": name,
        "mode": agent["mode"],
        "ok": return_code == 0 and final_ok and not timed_out and not stale_heartbeat,
        "return_code": return_code,
        "timed_out": timed_out,
        "stale_heartbeat": stale_heartbeat,
        "terminated_after_final": completed_from_final,
        "seconds": round(time.monotonic() - started, 2),
        "final_file": str(final_file),
        "stdout_file": str(stdout_file),
        "stderr_file": str(stderr_file),
        "error_class": error_class,
        "attempt": attempt,
    }
    if not result["ok"]:
        harmless = "/Users/michael/bin/ocodex:25: device not configured: /dev/tty"
        stderr_lines = [
            line for line in stderr_text.splitlines()
            if line.strip() and line.strip() != harmless
        ]
        result["error_tail"] = stderr_lines[-12:]
    stdout_text = ""
    if stdout_file.exists():
        try:
            stdout_text = stdout_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stdout_text = ""
    meta = parse_codex_jsonl(stdout_text)
    if meta.get("session_id"):
        result["session_id"] = meta["session_id"]
        write_json(session_path(out_dir, name), {
            "name": name,
            "session_id": meta["session_id"],
            "usage": meta.get("usage"),
            "attempt": attempt,
        })
    if meta.get("usage"):
        result["usage"] = meta["usage"]
    result["resumed_session"] = bool(session_id)
    return result


def run_one(
    agent: dict[str, Any],
    workdir: Path,
    out_dir: Path,
    default_model: str | None,
    ocodex_bin: str,
) -> dict[str, Any]:
    name = agent["name"]
    checkpoint_out = batch_out_dir(out_dir)
    seed_progress_files(checkpoint_out, [agent])
    result_path = checkpoint_out / f"{name}.result.json"

    provider = pick_provider(agent)
    bin_path, extra_env, extra_args = resolve_worker_bin(provider, ocodex_bin)
    candidates = model_candidates(agent, default_model)
    model_one = candidates[0] if candidates else default_model
    first = _run_attempt(
        agent, workdir, out_dir, model_one, bin_path, 1,
        extra_env=extra_env, extra_args=extra_args,
        session_id=None,
    )
    first["model"] = model_one
    first["provider"] = provider.get("name")
    attempts = [first]
    retried = False
    current = first
    # Harness-level retry once for empty output / stream-fail / crash / stale heartbeat.
    # Fatal (auth/sandbox/missing binary) is not retried. Retry uses the next model
    # candidate when one exists, still from the existing checkpoint.
    if not first["ok"] and should_retry(first, 0):
        retried = True
        merge_result_json(
            result_path,
            agent=agent,
            result={**first, "retries": 1, "retrying": True, "ok": False},
        )
        resume_id = first.get("session_id") or load_session_id(out_dir, name)
        # Same session → same model. Only hop models when we have no session to resume.
        model_two = model_one if resume_id else (
            candidates[1] if len(candidates) > 1 else model_one
        )
        second = _run_attempt(
            agent, workdir, out_dir, model_two, bin_path, 2,
            extra_env=extra_env, extra_args=extra_args,
            session_id=resume_id,
        )
        second["model"] = model_two
        second["provider"] = provider.get("name")
        attempts.append(second)
        current = second

    total_seconds = round(sum(item["seconds"] for item in attempts), 2)
    current = dict(current)
    current["retries"] = 1 if retried else 0
    current["attempts"] = len(attempts)
    current["seconds"] = total_seconds
    current["first_error_class"] = first.get("error_class") if retried else None
    current["retrying"] = False
    if current["ok"]:
        current["error_class"] = None

    final_text = ""
    final_file = Path(current.get("final_file") or "")
    if final_file.exists():
        try:
            final_text = final_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            final_text = ""
    parsed_claims = parse_claims(final_text)
    current["claims_parsed"] = parsed_claims
    current["schema_compliant"] = bool(parsed_claims)
    current["inferred_count"] = sum(1 for row in parsed_claims if row.get("tag") == "I")

    stats_record = {
        "ts": utc_now(),
        "name": name,
        "effort": agent.get("effort"),
        "model": current.get("model") or agent.get("model") or default_model,
        "provider": current.get("provider") or (provider.get("name") if provider else None),
        "ok": current["ok"],
        "return_code": current["return_code"],
        "seconds": total_seconds,
        "error_class": current.get("error_class") or current.get("first_error_class"),
        "retries": current["retries"],
        "attempt_codes": [item["return_code"] for item in attempts],
        "attempt_error_class": [item.get("error_class") for item in attempts],
        "slot": (extra_env or {}).get("OCODEX_KEY_SLOT") or os.environ.get("OCODEX_KEY_SLOT") or os.environ.get("OPENROUTER_SLOT"),
        "mode": agent.get("mode"),
        "schema_compliant": current["schema_compliant"],
        "inferred_count": current["inferred_count"],
        "session_id": current.get("session_id") or first.get("session_id"),
        "resumed_session": bool(current.get("resumed_session")),
        "usage": current.get("usage") or first.get("usage"),
    }
    append_stats(stats_record, out_dir / "stats.jsonl", checkpoint_out / "stats.jsonl")

    merged = merge_result_json(result_path, agent=agent, result=current)
    if parsed_claims and not merged.get("claims"):
        merged["claims"] = [row["claim"] for row in parsed_claims]
        merged["claims_parsed"] = parsed_claims
        merged["schema_compliant"] = True
        result_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    elif parsed_claims:
        merged["claims_parsed"] = parsed_claims
        merged["schema_compliant"] = True
        result_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    learned = normalize_learned(merged.get("learned"))
    if learned:
        merged["learned"] = learned
        result_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        harvest_learned(learned)
    current["result_file"] = str(result_path)
    return current


def _install_signal_handlers() -> None:
    def on_signal(signum, frame) -> None:
        kill_live_children()
        sys.exit(128 + int(signum) if isinstance(signum, int) else 1)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)


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
        workdir, agents = validate_manifest(load_manifest(args.manifest), args.timeout)
    except (ManifestError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.out_dir:
        out_root = args.out_dir.expanduser().resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        out_dir = Path(tempfile.mkdtemp(prefix="batch-", dir=out_root))
    else:
        out_dir = Path(tempfile.mkdtemp(prefix="ocodex-subagents-"))

    if args.dry_run:
        plan = []
        for agent in agents:
            final_file = out_dir / f"{agent['name']}.final.txt"
            plan.append({
                "name": agent["name"],
                "mode": agent["mode"],
                "command": command_for(agent, workdir, final_file, args.model, ocodex_bin),
                "prompt_preview": compact_brief(
                    agent, workdir,
                    checkpoint_out=batch_out_dir(out_dir),
                    result_path=batch_out_dir(out_dir) / f"{agent['name']}.result.json",
                ),
            })
        print(json.dumps({"out_dir": str(out_dir), "dry_run": True, "agents": plan}, indent=2))
        return 0

    _install_signal_handlers()
    worker_count = min(args.max_parallel, len(agents))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        delay = stagger_sec()
        futures = []
        for i, agent in enumerate(agents):
            if i and delay > 0:
                time.sleep(delay)
            futures.append(pool.submit(run_one, agent, workdir, out_dir, args.model, ocodex_bin))
        results = [future.result() for future in futures]

    summary = {"out_dir": str(out_dir), "ok": all(item["ok"] for item in results), "agents": results}
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
