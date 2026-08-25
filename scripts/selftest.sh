#!/bin/sh
# 6 agents, cap 2; agent 4 dies twice. Pool must refill, retry once, exit 1.
# Also checks live status.txt and a blocked `after` dependency.
set -e
cd "$(dirname "$0")"
python3 -m py_compile launch_batches.py run_agents.py fleet_state.py fleet_watch.py classify.py contextpack.py

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
python3 - "$TMP/manifest.json" <<'PY'
import json, sys
from pathlib import Path
agents = []
for i in range(1, 7):
    task = f"Do unit test {i}."
    if i == 4:
        task += " FAKE_FAIL"
    agents.append({"name": f"unit{i}", "task": task, "mode": "scout"})
agents.append({
    "name": "needs4",
    "task": "Should never start.",
    "mode": "scout",
    "after": ["unit4"],
})
Path(sys.argv[1]).write_text(json.dumps({"workdir": str(Path.cwd()), "agents": agents}))
PY
set +e
OCODEX_RUNNER="$PWD/fake_runner.py" OCODEX_POOL_POLL=0.05 OCODEX_STATS="$TMP/stats.jsonl" \
  python3 launch_batches.py "$TMP/manifest.json" --out-dir "$TMP/out" --max-workers 2 --no-watch
RC=$?
set -e
[ "$RC" = "1" ] || { echo "unexpected exit $RC"; exit 1; }
test -f "$TMP/out/all.done"
grep -q 'failed:needs4,unit4' "$TMP/out/all.done" || grep -q 'failed:unit4,needs4' "$TMP/out/all.done"
grep -q "Resume from the checkpoint" "$TMP/out/unit4-retry.manifest.json"
grep -q "halfway through unit4" "$TMP/out/unit4-retry.manifest.json"
test -f "$TMP/out/unit4-retry.failed"
test -f "$TMP/out/unit1.done"
test -f "$TMP/out/unit6.done"
test -f "$TMP/out/needs4.failed"
test -f "$TMP/out/status.txt"
test -f "$TMP/out/status.json"
python3 fleet_watch.py "$TMP/out" --once | grep -q unit1
python3 fleet_watch.py "$TMP/out" --once | grep -q needs4
# six first attempts + one retry = 7 stat lines (needs4 never launched)
python3 -c "import pathlib,sys; n=len(pathlib.Path('$TMP/stats.jsonl').read_text().splitlines()); sys.exit(0 if n==7 else (print(n) or 1))"
echo SELFTEST_OK
