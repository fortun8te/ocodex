#!/usr/bin/env python3
"""Unit tests for capacity, claims, providers, triage, classify. No live APIs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
LAUNCH = SCRIPTS / "launch_batches.py"
MANAGED = SCRIPTS / "ocodex_managed.py"
FAKE = Path(__file__).resolve().parent / "fake_ocodex.py"
RUNNER = SCRIPTS / "run_agents.py"

sys.path.insert(0, str(SCRIPTS))
import capacity  # noqa: E402
import claims  # noqa: E402
import classify  # noqa: E402
import harness_lib  # noqa: E402
import openrouter  # noqa: E402
import providers  # noqa: E402
import triage  # noqa: E402


def env_for(tmp: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OCODEX_BIN"] = str(FAKE)
    env["OCODEX_RUNNER"] = str(RUNNER)
    env["OCODEX_POLL_INTERVAL"] = "0.1"
    env["OCODEX_CAP_REFRESH_SEC"] = "0.05"
    env["ORSLOT_BIN"] = str(tmp / "no-such-orslot")
    env["OCODEX_MIDNIGHT_WINDOW_SEC"] = "0"
    env.pop("OCODEX_STATS", None)
    env.pop("OCODEX_SCOUT_PROVIDER", None)
    env.pop("OCODEX_PROVIDER", None)
    env.pop("OCODEX_PROVIDERS", None)
    env.update(extra)
    return env


ORSLOT_SAMPLE = """today  ████████  3001/4000  resets in 2h (UTC)

  1    ····················  0/1000  …1b0e
  2    ████████████████████  1001/1000  spent  …ee3a
  3    ████████████████████  1000/1000  spent  …441c
* 4    ████████████████████  1000/1000  spent  …bb1d
"""


class ParsePoolTests(unittest.TestCase):
    def test_counts_only_live_keys(self):
        keys, remaining = capacity.parse_slot_pool(ORSLOT_SAMPLE)
        self.assertEqual(keys, 1)
        self.assertEqual(remaining, 1000)

    def test_all_spent_is_zero_not_inflated(self):
        text = "today 4000/4000\n  1  1000/1000  spent\n  2  1000/1000  spent\n"
        keys, remaining = capacity.parse_slot_pool(text)
        self.assertEqual(keys, 0)
        self.assertEqual(remaining, 0)

    def test_unparseable_does_not_dry_the_pool(self):
        keys, remaining = capacity.parse_slot_pool("hello world")
        self.assertEqual(keys, 1)
        self.assertEqual(remaining, 10**9)


class CapacityMonitorTests(unittest.TestCase):
    def test_live_refresh_and_drop_guard(self):
        probes = [(4, 2000), (1, 500), (1, 400)]
        def probe():
            return probes.pop(0)
        mon = capacity.CapacityMonitor(probe, workers_per_key=6, refresh_sec=0)
        self.assertEqual(mon.keys, 4)
        self.assertEqual(mon.cap(), 24)
        # 50%+ drop is ignored once
        snap = mon.refresh(force=True)
        self.assertEqual(snap["keys"], 4)
        # second consecutive drop is trusted
        snap = mon.refresh(force=True)
        self.assertEqual(snap["keys"], 1)
        self.assertEqual(mon.cap(), 6)

    def test_dry_pool_cap_zero(self):
        mon = capacity.CapacityMonitor(lambda: (0, 0), workers_per_key=6, refresh_sec=0)
        self.assertEqual(mon.cap(), 0)
        self.assertTrue(mon.snapshot()["dry"])


class ClaimsTests(unittest.TestCase):
    def test_parse_and_check(self):
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw)
            (work / "README.md").write_text("# ocodex\nA parallel cheap-worker harness\n")
            text = (
                'CLAIM | C | title is ocodex | README.md:1 | "# ocodex"\n'
                'CLAIM | I | maybe a dragon | README.md:99 | "nope"\n'
                'CLAIM | C | fabricated | missing.py:1 | "ghost"\n'
            )
            rows = claims.parse_claims(text)
            self.assertEqual(len(rows), 3)
            report = claims.check_text(text, work)
            self.assertTrue(report["schema_compliant"])
            self.assertTrue(report["evidence_failed"])
            verdicts = {row["claim"]: row["verdict"] for row in report["claims"]}
            self.assertEqual(verdicts["title is ocodex"], "ok")
            self.assertEqual(verdicts["maybe a dragon"], "inferred")
            self.assertEqual(verdicts["fabricated"], "missing")


class ProviderTests(unittest.TestCase):
    def test_explicit_model_wins(self):
        cands = providers.model_candidates({"name": "a", "mode": "scout", "model": "my/model"})
        self.assertEqual(cands, ["my/model"])

    def test_scout_defaults_cheap(self):
        table = providers.DEFAULT_PROVIDERS
        with patch.object(providers, "muse_auth_present", return_value=True):
            cands = providers.model_candidates(
                {"name": "a", "mode": "scout"}, providers=table,
            )
        self.assertEqual(cands[0], providers.MUSE_MODEL)

    def test_default_provider_is_muse(self):
        table = providers.DEFAULT_PROVIDERS
        with patch.object(providers, "muse_auth_present", return_value=True):
            picked = providers.pick_provider({"name": "a", "mode": "scout"}, providers=table)
        self.assertEqual(picked["name"], "muse")

    def test_effort_split(self):
        self.assertEqual(providers.default_effort({"mode": "scout"}), "low")
        self.assertEqual(providers.default_effort({"mode": "worker"}), "medium")
        self.assertEqual(providers.default_effort({"mode": "worker", "effort": "high"}), "high")

    def test_groq_only_when_asked(self):
        table = providers.DEFAULT_PROVIDERS
        with patch.object(providers, "muse_auth_present", return_value=True):
            picked = providers.pick_provider({"name": "a", "mode": "scout"}, providers=table)
            self.assertEqual(picked["name"], "muse")
        old = os.environ.get("OCODEX_SCOUT_PROVIDER")
        os.environ["OCODEX_SCOUT_PROVIDER"] = "groq"
        os.environ["GROQ_API_KEY"] = "gsk-test"
        try:
            picked = providers.pick_provider(
                {"name": "a", "mode": "scout"}, providers=table,
            )
            self.assertEqual(picked["name"], "groq")
        finally:
            if old is None:
                os.environ.pop("OCODEX_SCOUT_PROVIDER", None)
            else:
                os.environ["OCODEX_SCOUT_PROVIDER"] = old
            os.environ.pop("GROQ_API_KEY", None)


class ClassifyRetryTests(unittest.TestCase):
    def test_fatal_not_retried(self):
        result = {"ok": False, "error_tail": ["invalid api key"], "return_code": 1}
        self.assertEqual(classify.classify(result), "fatal")
        self.assertFalse(classify.should_retry(result, 0))

    def test_stream_retried_once(self):
        result = {"ok": False, "error_tail": ["stream disconnect"], "return_code": 1}
        self.assertEqual(classify.classify(result), "transient")
        self.assertTrue(classify.should_retry(result, 0))
        self.assertFalse(classify.should_retry(result, 1))


class OpenRouterTests(unittest.TestCase):
    def test_key_snapshot_strips_secrets(self):
        payload = {
            "data": {
                "label": "sk-or-v1-abcdefghijklmnop",
                "is_free_tier": True,
                "usage_daily": 1.2,
                "limit_remaining": 8.0,
            }
        }
        with patch.object(openrouter, "fetch_json", return_value=payload):
            snap = openrouter.key_snapshot(api_key="SECRET")
        self.assertEqual(snap["label_suffix"], "mnop")
        self.assertNotIn("SECRET", json.dumps(snap))
        self.assertTrue(snap["is_free_tier"])


class TriageTests(unittest.TestCase):
    def test_crashed_does_not_escalate(self):
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw)
            work = out / "work"
            work.mkdir()
            harness_lib.write_json(out / "ledger.json", {
                "workdir": str(work),
                "modes": {"dead": "scout"},
                "ownership": {"dead": []},
                "chunk_plan": [{"chunk": 0, "agents": ["dead"]}],
            })
            harness_lib.write_json(out / "dead.result.json", {
                "ok": False, "status": "failed", "error_class": "stream_fail",
            })
            report = triage.collect_triage(out, work)
            self.assertEqual(report["agents"]["dead"]["verdict"], "crashed")
            self.assertEqual(report["crashed"], ["dead"])
            self.assertEqual(report["escalate"], [])


class SessionJsonlTests(unittest.TestCase):
    def test_parses_session_meta(self):
        text = (
            '{"type":"session_meta","payload":{"session_id":"11111111-1111-1111-1111-111111111111"}}\n'
            '{"type":"item","payload":{"usage":{"input_tokens":9,"output_tokens":3}}}\n'
        )
        meta = harness_lib.parse_codex_jsonl(text)
        self.assertEqual(meta["session_id"], "11111111-1111-1111-1111-111111111111")
        self.assertEqual(meta["usage"]["input_tokens"], 9)

    def test_unrelated_uuid_in_stdout_is_not_session_id(self):
        noise = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        text = (
            f"stream warning id={noise} not a session\n"
            f'{{"type":"item","payload":{{"text":"see {noise}"}}}}\n'
        )
        meta = harness_lib.parse_codex_jsonl(text)
        self.assertIsNone(meta["session_id"])


class ScoutTimeoutTests(unittest.TestCase):
    def test_scout_defaults_to_300(self):
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw)
            _, agents = harness_lib.validate_manifest({
                "workdir": str(work),
                "agents": [{"name": "s", "mode": "scout", "task": "look"}],
            }, default_timeout=900)
            self.assertEqual(agents[0]["timeout"], 300)

    def test_worker_keeps_default(self):
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw)
            (work / "x.py").write_text("x\n")
            _, agents = harness_lib.validate_manifest({
                "workdir": str(work),
                "agents": [{"name": "w", "mode": "worker", "owns": ["x.py"], "task": "edit"}],
            }, default_timeout=900)
            self.assertEqual(agents[0]["timeout"], 900)


class MidnightPauseTests(unittest.TestCase):
    def test_window_zero_never_pauses(self):
        old = os.environ.get("OCODEX_MIDNIGHT_WINDOW_SEC")
        os.environ["OCODEX_MIDNIGHT_WINDOW_SEC"] = "0"
        try:
            from capacity import pause_for_utc_reset
            self.assertFalse(pause_for_utc_reset())
        finally:
            if old is None:
                os.environ.pop("OCODEX_MIDNIGHT_WINDOW_SEC", None)
            else:
                os.environ["OCODEX_MIDNIGHT_WINDOW_SEC"] = old


class SearchPackTests(unittest.TestCase):
    def test_packs_read_only_scouts_by_file(self):
        from search_pack import pack_manifest
        hits = [
            {"kind": "file", "path": "a.md", "line": 1, "text": "alpha"},
            {"kind": "file", "path": "a.md", "line": 2, "text": "alpha two"},
            {"kind": "file", "path": "b.md", "line": 1, "text": "beta"},
            {"kind": "file", "path": "c.md", "line": 1, "text": "gamma"},
        ]
        packed = pack_manifest("alpha", hits, workdir="/tmp/work", max_scouts=2)
        self.assertEqual(packed["scout_count"], 2)
        for agent in packed["agents"]:
            self.assertEqual(agent["mode"], "scout")
            self.assertIn("READ-ONLY", agent["task"])
            self.assertIn("Do not edit", agent["task"])
            self.assertTrue(agent.get("owns"))

    def test_local_harvest_and_dry_run_search(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work, out = root / "work", root / "out"
            work.mkdir()
            (work / "notes.md").write_text("the secret mango recipe lives here\n")
            (work / "other.md").write_text("nothing relevant\n")
            proc = subprocess.run(
                [sys.executable, str(MANAGED), "search", "mango",
                 "--workdir", str(work), "--out-dir", str(out), "--dry-run",
                 "--max-scouts", "3"],
                capture_output=True, text=True, env=env_for(root),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("read-only", proc.stdout.lower())
            man = json.loads((out / "manifest.json").read_text())
            self.assertTrue(man["agents"])
            self.assertTrue(all(a["mode"] == "scout" for a in man["agents"]))
            hits = json.loads((out / "hits.json").read_text())
            self.assertGreaterEqual(hits["hit_count"] if "hit_count" in hits else len(hits["hits"]), 1)
            self.assertIn("mango", hits["hits"][0]["text"].lower())


class CompactBriefClaimsTests(unittest.TestCase):
    def test_brief_requires_claim_lines(self):
        brief = harness_lib.compact_brief(
            {"name": "x", "mode": "scout", "task": "look", "owns": []},
            Path("/tmp/work"),
            checkpoint_out=Path("/tmp/out"),
            result_path=Path("/tmp/out/x.result.json"),
        )
        self.assertIn("CLAIM | C or I", brief)


class PoolLaunchTests(unittest.TestCase):
    def test_three_agents_one_per_chunk(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work, out = root / "work", root / "out"
            work.mkdir()
            man = root / "m.json"
            man.write_text(json.dumps({
                "workdir": str(work),
                "agents": [
                    {"name": "a", "mode": "scout", "task": "a", "timeout": 30},
                    {"name": "b", "mode": "scout", "task": "b", "timeout": 30},
                    {"name": "c", "mode": "scout", "task": "c", "timeout": 30},
                ],
            }))
            proc = subprocess.run(
                [sys.executable, str(LAUNCH), str(man), "--out-dir", str(out),
                 "--max-workers", "2", "--timeout", "30"],
                capture_output=True, text=True, env=env_for(root),
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual((out / "all.done").read_text().strip(), "done")
            self.assertTrue((out / "chunk-0.done").exists())
            self.assertTrue((out / "chunk-1.done").exists())
            self.assertTrue((out / "chunk-2.done").exists())
            self.assertTrue((out / "triage.json").exists())
            ledger = json.loads((out / "ledger.json").read_text())
            self.assertEqual(ledger.get("scheduler"), "pool")
            self.assertEqual(len(ledger["chunk_plan"]), 3)

    def test_fatal_not_retried(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work, out = root / "work", root / "out"
            work.mkdir()
            man = root / "m.json"
            man.write_text(json.dumps({
                "workdir": str(work),
                "agents": [{"name": "nope", "mode": "scout", "task": "auth", "timeout": 30}],
            }))
            count = root / "count"
            proc = subprocess.run(
                [sys.executable, str(LAUNCH), str(man), "--out-dir", str(out), "--timeout", "30"],
                capture_output=True, text=True,
                env=env_for(root, FAKE_OCODEX_MODE="fail_fatal", FAKE_OCODEX_COUNT=str(count)),
            )
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertTrue(count.exists())
            self.assertEqual(int(count.read_text().strip()), 1)
            tri = json.loads((out / "triage.json").read_text())
            self.assertEqual(tri["agents"]["nope"]["verdict"], "crashed")


if __name__ == "__main__":
    unittest.main()
