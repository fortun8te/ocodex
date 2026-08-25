#!/usr/bin/env python3
"""Harness tests. No live ocodex / OpenRouter / SearXNG required."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
LAUNCH = SCRIPTS / "launch_batches.py"
RUNNER = SCRIPTS / "run_agents.py"
MANAGED = SCRIPTS / "ocodex_managed.py"
WAIT = SCRIPTS / "wait_done.py"
FAKE = Path(__file__).resolve().parent / "fake_ocodex.py"

sys.path.insert(0, str(SCRIPTS))
import harness_lib  # noqa: E402


def env_for(tmp: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OCODEX_BIN"] = str(FAKE)
    env["OCODEX_RUNNER"] = str(RUNNER)
    env["OCODEX_POLL_INTERVAL"] = "0.1"
    env.pop("OCODEX_STATS", None)
    env.update(extra)
    return env


def write_manifest(path: Path, workdir: Path, agents: list[dict]) -> Path:
    path.write_text(json.dumps({"workdir": str(workdir), "agents": agents}, indent=2))
    return path


def pids_mentioning(needle: str, exclude: set[int] | None = None) -> list[int]:
    exclude = exclude or set()
    found: list[int] = []
    proc = Path("/proc")
    if not proc.exists():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in exclude or pid == os.getpid():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        if needle.encode() in cmd:
            found.append(pid)
    return found


class CheckpointTests(unittest.TestCase):
    def test_clause_is_step_zero_with_heartbeat(self):
        clause = harness_lib.checkpoint_clause("/tmp/out", "fix-parser")
        self.assertIn("STEP ZERO", clause)
        self.assertIn("BEFORE editing any owned file", clause)
        self.assertIn("per named sub-task", clause)
        self.assertIn("HEARTBEAT", clause)
        self.assertIn("2 minutes", clause)
        self.assertTrue(clause.startswith("STEP ZERO"))


class OwnsTests(unittest.TestCase):
    def test_overlap_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw)
            data = {
                "workdir": str(work),
                "agents": [
                    {"name": "a", "mode": "worker", "owns": ["src"], "task": "edit a"},
                    {"name": "b", "mode": "worker", "owns": ["src/parser.ts"], "task": "edit b"},
                ],
            }
            with self.assertRaises(harness_lib.ManifestError) as ctx:
                harness_lib.validate_manifest(data)
            self.assertIn("ownership overlap", str(ctx.exception))

    def test_launch_overlap_exit_2(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work, out = root / "work", root / "out"
            work.mkdir()
            man = write_manifest(root / "m.json", work, [
                {"name": "a", "mode": "worker", "owns": ["x.py"], "task": "a", "timeout": 30},
                {"name": "b", "mode": "worker", "owns": ["x.py"], "task": "b", "timeout": 30},
            ])
            proc = subprocess.run(
                [sys.executable, str(LAUNCH), str(man), "--out-dir", str(out)],
                capture_output=True, text=True, env=env_for(root),
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("ownership overlap", proc.stderr)


class CapacityTests(unittest.TestCase):
    def test_workers_per_key_overridable_not_clamped(self):
        plan = harness_lib.plan_capacity(6, keys=1, remaining=1000, workers_per_key=5, max_workers=10)
        self.assertEqual(plan["cap"], 10)
        self.assertTrue(plan["over_recommend"])
        text = harness_lib.format_headroom(plan, 40)
        self.assertIn("concurrency cap", text)
        self.assertIn("not a crash", text.lower() + text)


class StatusBoardTests(unittest.TestCase):
    def test_parses_heartbeat_and_marks_stale(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)
            now = datetime.now(timezone.utc)
            stale_ts = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
            fresh_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            started = (now - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
            harness_lib.write_json(out / "ledger.json", {
                "started_at": started,
                "out": str(out),
                "modes": {"alive-agent": "worker", "stale-agent": "scout"},
                "tasks": {"alive-agent": "fix parser", "stale-agent": "hunt bugs"},
                "models": {"alive-agent": "openrouter/cheap", "stale-agent": "openrouter/cheap"},
                "chunk_plan": [{"chunk": 0, "agents": ["alive-agent", "stale-agent"], "status": "pending"}],
            })
            (out / "alive-agent.progress.md").write_text(
                f"# alive-agent\nHEARTBEAT {fresh_ts} | write-tests | editing src/parser.ts\n"
            )
            (out / "stale-agent.progress.md").write_text(
                f"# stale-agent\nHEARTBEAT {stale_ts} | scan-src | reading src/\n"
            )
            rows = {row["name"]: row for row in harness_lib.collect_status(out, now=now)}
            self.assertIn("alive-agent", rows)
            self.assertIn("stale-agent", rows)
            self.assertEqual(rows["stale-agent"]["state"], "STALE")
            self.assertEqual(rows["alive-agent"]["state"], "alive")
            self.assertEqual(rows["alive-agent"]["subtask"], "write-tests")
            table = harness_lib.format_status_table(list(rows.values()), out=out)
            self.assertIn("alive-agent", table)
            self.assertIn("STALE", table)
            proc = subprocess.run(
                [sys.executable, str(MANAGED), "status", str(out)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("stale-agent", proc.stdout)
            self.assertIn("HEARTBEAT", proc.stdout + "heartbeat")
            self.assertIn("STALE", proc.stdout)


class DoctorTests(unittest.TestCase):
    def test_reports_missing_searxng(self):
        env = os.environ.copy()
        env["SEARXNG_URL"] = "http://127.0.0.1:1"
        proc = subprocess.run(
            [sys.executable, str(MANAGED), "doctor"],
            capture_output=True, text=True, env=env,
        )
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("searxng", combined.lower())
        self.assertIn("MISS", combined)


class LauncherTests(unittest.TestCase):
    def test_stale_marker_refusal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work, out = root / "work", root / "out"
            work.mkdir()
            out.mkdir()
            (out / "all.done").write_text("done")
            man = write_manifest(root / "m.json", work, [
                {"name": "x", "mode": "scout", "task": "look", "timeout": 30},
            ])
            proc = subprocess.run(
                [sys.executable, str(LAUNCH), str(man), "--out-dir", str(out)],
                capture_output=True, text=True, env=env_for(root),
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("refusing to run", proc.stderr)

    def test_all_done_failure_signaling(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work, out = root / "work", root / "out"
            work.mkdir()
            man = write_manifest(root / "m.json", work, [
                {"name": "doomed", "mode": "scout", "task": "fail please", "timeout": 30},
            ])
            count = root / "count"
            proc = subprocess.run(
                [sys.executable, str(LAUNCH), str(man), "--out-dir", str(out), "--timeout", "30"],
                capture_output=True, text=True,
                env=env_for(root, FAKE_OCODEX_MODE="fail_always", FAKE_OCODEX_COUNT=str(count)),
            )
            self.assertEqual(proc.returncode, 1)
            self.assertTrue((out / "all.done").exists())
            self.assertTrue((out / "all.done").read_text().startswith("failed:"))
            self.assertTrue((out / "chunk-0.done").exists())
            self.assertNotEqual((out / "chunk-0.done").read_text().strip(), "0")

    def test_retry_once_on_fail_then_success(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work, out = root / "work", root / "out"
            work.mkdir()
            man = write_manifest(root / "m.json", work, [
                {"name": "flaky", "mode": "scout", "task": "maybe", "timeout": 30},
            ])
            count = root / "count"
            stdin_dump = root / "stdin.txt"
            proc = subprocess.run(
                [sys.executable, str(LAUNCH), str(man), "--out-dir", str(out), "--timeout", "30"],
                capture_output=True, text=True,
                env=env_for(
                    root,
                    FAKE_OCODEX_MODE="fail_once",
                    FAKE_OCODEX_COUNT=str(count),
                    FAKE_OCODEX_STDIN=str(stdin_dump),
                ),
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual((out / "all.done").read_text().strip(), "done")
            self.assertTrue(count.exists())
            self.assertGreaterEqual(int(count.read_text().strip()), 2)
            stats = (out / "stats.jsonl").read_text()
            rec = json.loads(stats.strip().splitlines()[-1])
            self.assertEqual(rec["retries"], 1)
            self.assertTrue(rec["ok"])
            self.assertIn("STEP ZERO", stdin_dump.read_text())
            self.assertIn("HEARTBEAT", stdin_dump.read_text())
            self.assertIn("BEFORE editing", stdin_dump.read_text())

    def test_sigterm_kills_children_and_writes_done(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work, out = root / "work", root / "out"
            work.mkdir()
            man = write_manifest(root / "m.json", work, [
                {"name": "sleeper", "mode": "scout", "task": "sleep forever", "timeout": 30},
            ])
            pidfile = root / "fake.pid"
            env = env_for(
                root,
                FAKE_OCODEX_MODE="sleep",
                FAKE_OCODEX_SLEEP="60",
                FAKE_OCODEX_PIDFILE=str(pidfile),
            )
            proc = subprocess.Popen(
                [sys.executable, str(LAUNCH), str(man), "--out-dir", str(out), "--timeout", "30"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.time() + 10
            while time.time() < deadline:
                if (out / "chunk-0.runner.log").exists() and pidfile.exists():
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            time.sleep(0.2)
            self.assertIsNone(proc.poll(), "launcher exited before SIGTERM")
            needle = str(out)
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                self.fail("launcher did not exit after SIGTERM")
            leftover = pids_mentioning(needle, exclude={proc.pid})
            # Give the kernel a beat to reap.
            if leftover:
                time.sleep(0.5)
                leftover = pids_mentioning(needle, exclude={proc.pid})
            self.assertEqual(leftover, [], f"orphans still running: {leftover}")
            done = list(out.glob("chunk-*.done"))
            self.assertTrue(done, "expected chunk-*.done after SIGTERM")
            if pidfile.exists():
                fake_pid = int(pidfile.read_text().strip())
                self.assertFalse(Path(f"/proc/{fake_pid}").exists(), "fake ocodex still alive")


class WaitDoneTests(unittest.TestCase):
    def test_failed_all_done_exits_1(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)
            (out / "all.done").write_text("failed:0")
            (out / "ledger.json").write_text(json.dumps({"started_at": harness_lib.utc_now()}))
            proc = subprocess.run(
                [sys.executable, str(WAIT), str(out), "--timeout", "2", "--poll", "0.05"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 1)


class ManagedRunTests(unittest.TestCase):
    def test_dry_run_goal_writes_brief(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work, out = root / "work", root / "out"
            work.mkdir()
            proc = subprocess.run(
                [sys.executable, str(MANAGED), "run", "Fix the parser",
                 "--workdir", str(work), "--owns", "parser.ts",
                 "--out-dir", str(out), "--dry-run", "--name", "fix-parser"],
                capture_output=True, text=True, env=env_for(root),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("SUPERVISOR BRIEF", proc.stdout)
            self.assertIn("headroom", proc.stdout)
            self.assertTrue((out / "supervisor-brief.md").exists())
            self.assertTrue((out / "manifest.json").exists())
            man = json.loads((out / "manifest.json").read_text())
            self.assertEqual(man["agents"][0]["owns"], ["parser.ts"])


if __name__ == "__main__":
    unittest.main()
