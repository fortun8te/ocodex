#!/usr/bin/env python3
"""Test double for run_agents.py. Reads a manifest, writes results, exits 0
unless the task contains FAKE_FAIL (writes a checkpoint, then exits 1)."""
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
out = Path(sys.argv[3])
out.mkdir(parents=True, exist_ok=True)
agent = manifest["agents"][0]
name = agent["name"]
(out / f"{name}.final.txt").write_text(f"fake final reply for {name}\n")
(out / "live.json").write_text(json.dumps({
    "name": name,
    "state": "running",
    "stderr_tail": [f"working on {name}"],
}) + "\n")
(out / "results.json").write_text(json.dumps({
    "ok": "FAKE_FAIL" not in agent["task"],
    "agents": [{"name": name, "ok": "FAKE_FAIL" not in agent["task"], "class": "transient" if "FAKE_FAIL" in agent["task"] else "ok"}],
}))
if "FAKE_FAIL" in agent["task"]:
    prog = out.parent / f"{name}.progress.md"
    prog.write_text(
        f"## Done\n- [x] halfway through {name}\n\n"
        f"## Files touched\n\n"
        f"## Next step\nfinish {name}\n\n"
        f"## Open questions\n\n"
    )
    print(f"simulating provider death for {name}", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
