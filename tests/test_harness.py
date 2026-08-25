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
    env["ORSLOT_BIN"] = str(tmp / "no-such-orslot")
    env["OCODEX_MIDNIGHT_WINDOW_SEC"] = "0"
    env.pop("OCODEX_STATS", None)
    env.pop("OCODEX_SCOUT_PROVIDER", None)
    env.pop("OCODEX_PROVIDER", None)
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
        self.assertIn("HEARTBEAT <ISO-8601-Z> | <subtask> | <one-liner focus>", clause)
        self.assertTrue(clause.startswith("STEP ZERO"))
        self.assertLess(len(clause), 500)

    def test_first_attempt_prompt_has_contract_once(self):
        sys.path.insert(0, str(SCRIPTS))
        import run_agents
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)
            agent = {"name": "fix-parser", "mode": "scout", "task": "fix it", "owns": []}
            prompt = run_agents.build_prompt(agent, Path("/tmp/work"), out)
            self.assertEqual(prompt.count("STEP ZERO"), 1)
            self.assertEqual(prompt.count("OWNS:"), 1)
            self.assertEqual(prompt.count("STOP:"), 1)
            self.assertEqual(prompt.count("CLAIM | C or I"), 1)
            self.assertEqual(
                prompt.count("HEARTBEAT <ISO-8601-Z> | <subtask> | <one-liner focus>"), 1,
            )
            leftover = {
                **agent,
                "task": harness_lib.checkpoint_clause(out, "fix-parser") + "\n\nfix it",
            }
            stripped = run_agents.build_prompt(leftover, Path("/tmp/work"), out)
            self.assertEqual(stripped.count("STEP ZERO"), 1)
            self.assertIn("fix it", stripped.split("TASK:")[-1])
            self.assertNotIn(harness_lib.CHECKPOINT_END, stripped.split("TASK:")[-1])

    def test_resume_clause_does_not_start_over(self):
        clause = harness_lib.resume_clause("/tmp/out", "fix-parser")
        self.assertTrue(clause.startswith("RESUME"))
        self.assertIn("do not start over", clause.lower())
        self.assertIn("last HEARTBEAT", clause)

    def test_compact_brief_resume_includes_checkpoint_excerpt(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)
            lines = [f"# fix-parser checkpoint"]
            lines += [f"- bullet {i}" for i in range(25)]
            lines.append("HEARTBEAT 2026-08-25T16:00:00Z | parse-errors | reading src/parser.ts")
            (out / "fix-parser.progress.md").write_text("\n".join(lines) + "\n")
            brief = harness_lib.compact_brief(
                {"name": "fix-parser", "mode": "scout", "task": "fix it", "owns": []},
                Path("/tmp/work"),
                checkpoint_out=out,
                result_path=out / "fix-parser.result.json",
                resume=True,
            )
            self.assertTrue(brief.startswith("RESUME"))
            self.assertIn("do not start over", brief.lower())
            self.assertIn("Last checkpoint excerpt:", brief)
            self.assertIn("parse-errors", brief)
            self.assertIn("reading src/parser.ts", brief)
            self.assertIn("bullet 24", brief)
            self.assertNotIn("bullet 0", brief)  # only last ~20 lines
            header = brief.split("TASK:")[0]
            self.assertFalse(header.strip().startswith("STEP ZERO"))
            normal = harness_lib.compact_brief(
                {"name": "fix-parser", "mode": "scout", "task": "fix it", "owns": []},
                Path("/tmp/work"),
                checkpoint_out=out,
                result_path=out / "fix-parser.result.json",
            )
            self.assertTrue(normal.startswith("STEP ZERO"))
            self.assertNotIn("Last checkpoint excerpt:", normal)


class StaleHeartbeatTests(unittest.TestCase):
    def test_progress_is_stale(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "a.progress.md"
            now = datetime(2026, 8, 25, 17, 0, 0, tzinfo=timezone.utc)
            # Missing file / missing HEARTBEAT → stale (grace is the wait-loop's job).
            self.assertTrue(harness_lib.progress_is_stale(path, now=now))
            path.write_text("# title only\n")
            self.assertTrue(harness_lib.progress_is_stale(path, now=now))
            path.write_text("HEARTBEAT 2026-08-25T16:59:00Z | work | still going\n")
            self.assertFalse(harness_lib.progress_is_stale(path, now=now))  # 60s < 120s
            path.write_text("HEARTBEAT 2026-08-25T16:57:00Z | work | stuck\n")
            self.assertTrue(harness_lib.progress_is_stale(path, now=now))  # 180s > 120s

    def test_is_stale_heartbeat_requires_attempt_elapsed(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "a.progress.md"
            now_wall = datetime(2026, 8, 25, 17, 0, 0, tzinfo=timezone.utc)
            path.write_text("HEARTBEAT 2026-08-25T16:57:00Z | work | stuck\n")
            self.assertFalse(harness_lib.is_stale_heartbeat(
                path, attempt_started=0.0, now=10.0, wall_now=now_wall, interval=120,
            ))
            self.assertTrue(harness_lib.is_stale_heartbeat(
                path, attempt_started=0.0, now=200.0, wall_now=now_wall, interval=120,
            ))

    def test_ocodex_heartbeat_sec_env(self):
        old = os.environ.get("OCODEX_HEARTBEAT_SEC")
        self.addCleanup(self._restore_hb, old)
        os.environ["OCODEX_HEARTBEAT_SEC"] = "1"
        self.assertEqual(harness_lib.heartbeat_interval_sec(), 1.0)
        os.environ["OCODEX_HEARTBEAT_SEC"] = "0"
        self.assertEqual(harness_lib.heartbeat_interval_sec(), 120.0)
        os.environ["OCODEX_HEARTBEAT_SEC"] = "nope"
        self.assertEqual(harness_lib.heartbeat_interval_sec(), 120.0)

    def _restore_hb(self, old):
        if old is None:
            os.environ.pop("OCODEX_HEARTBEAT_SEC", None)
        else:
            os.environ["OCODEX_HEARTBEAT_SEC"] = old

    def test_classify_stale_heartbeat(self):
        self.assertEqual(
            harness_lib.classify_error(143, False, False, "", stale_heartbeat=True),
            "stale_heartbeat",
        )
        self.assertEqual(
            harness_lib.classify_error(124, True, False, ""),
            "timeout",
        )


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

    def test_retrying_state_then_dead_after_failed_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)
            now = datetime.now(timezone.utc)
            started = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            stale_ts = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
            harness_lib.write_json(out / "ledger.json", {
                "started_at": started,
                "out": str(out),
                "modes": {"flaky": "worker"},
                "tasks": {"flaky": "fix parser"},
                "models": {"flaky": "openrouter/cheap"},
                "chunk_plan": [{"chunk": 0, "agents": ["flaky"], "status": "pending"}],
            })
            (out / "flaky.progress.md").write_text(
                f"# flaky\nHEARTBEAT {stale_ts} | write-tests | stuck\n"
            )
            harness_lib.write_json(out / "flaky.result.json", {
                "status": "failed", "ok": False, "retries": 1, "retrying": True,
            })
            rows = {row["name"]: row for row in harness_lib.collect_status(out, now=now)}
            self.assertEqual(rows["flaky"]["state"], "retrying")
            harness_lib.write_json(out / "flaky.result.json", {
                "status": "failed", "ok": False, "retries": 1, "retrying": False,
            })
            rows = {row["name"]: row for row in harness_lib.collect_status(out, now=now)}
            self.assertEqual(rows["flaky"]["state"], "dead")
            table = harness_lib.format_status_table(list(rows.values()), out=out)
            self.assertIn("dead", table)


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
            argv_dump = root / "argv.txt"
            proc = subprocess.run(
                [sys.executable, str(LAUNCH), str(man), "--out-dir", str(out), "--timeout", "30"],
                capture_output=True, text=True,
                env=env_for(
                    root,
                    FAKE_OCODEX_MODE="fail_once",
                    FAKE_OCODEX_COUNT=str(count),
                    FAKE_OCODEX_STDIN=str(stdin_dump),
                    FAKE_OCODEX_ARGV=str(argv_dump),
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
            stdin = stdin_dump.read_text()
            self.assertIn("STEP ZERO", stdin)
            self.assertIn("HEARTBEAT", stdin)
            self.assertIn("BEFORE editing", stdin)
            self.assertIn("RESUME", stdin)
            self.assertIn("do not start over", stdin.lower())
            argv = argv_dump.read_text()
            self.assertIn("11111111-1111-1111-1111-111111111111", argv)
            self.assertIn(" resume ", f" {argv} ")
            self.assertNotIn("--ephemeral", argv)
            session = json.loads((out / "flaky.session.json").read_text())
            self.assertEqual(session["session_id"], "11111111-1111-1111-1111-111111111111")

    def test_stale_heartbeat_kills_and_retries_from_checkpoint(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work, out = root / "work", root / "out"
            work.mkdir()
            man = write_manifest(root / "m.json", work, [
                {"name": "zombie", "mode": "scout", "task": "sleep without heartbeats", "timeout": 30},
            ])
            count = root / "count"
            stdin_dump = root / "stdin.txt"
            proc = subprocess.run(
                [sys.executable, str(LAUNCH), str(man), "--out-dir", str(out), "--timeout", "30"],
                capture_output=True, text=True, timeout=20,
                env=env_for(
                    root,
                    FAKE_OCODEX_MODE="sleep",
                    FAKE_OCODEX_SLEEP="60",
                    FAKE_OCODEX_COUNT=str(count),
                    FAKE_OCODEX_STDIN=str(stdin_dump),
                    OCODEX_HEARTBEAT_SEC="1",
                ),
            )
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertTrue(count.exists())
            self.assertGreaterEqual(int(count.read_text().strip()), 2)
            stats = json.loads((out / "stats.jsonl").read_text().strip().splitlines()[-1])
            self.assertEqual(stats["retries"], 1)
            self.assertFalse(stats["ok"])
            self.assertEqual(stats["error_class"], "stale_heartbeat")
            self.assertIn("stale_heartbeat", stats.get("attempt_error_class") or [])
            self.assertLess(stats["seconds"], 20)
            stdin = stdin_dump.read_text()
            self.assertIn("RESUME", stdin)
            self.assertIn("do not start over", stdin.lower())
            self.assertIn("last HEARTBEAT", stdin)
            progress = (out / "zombie.progress.md").read_text()
            self.assertIn("harness created this file", progress)
            result = json.loads((out / "zombie.result.json").read_text())
            self.assertFalse(result.get("ok"))
            self.assertFalse(result.get("retrying"))
            self.assertEqual(result.get("status"), "failed")

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
