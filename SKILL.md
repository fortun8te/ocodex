---
name: ocodex
description: "/ocodex <goal> — run any decomposable task on a fleet of free supervised parallel agents. Entry point for every harness (Claude Code, Codex, Grok, Cursor, desktop). Use for bug hunts, doc sync, test authoring, audits, mechanical fixes, parity chores — anything that splits into bounded chunks that are cheap to VERIFY — or whenever several subagents would be spawned at once."
---

# /ocodex — free supervised agent fleet, one command

Take the user's goal and run the whole pipeline: decompose → launch → supervise
→ integrate. Workers are free and fallible (~25% die; survivors ship confident
errors); the supervisor is the only quality gate. Never run unsupervised.

Scripts live in `scripts/` beside this SKILL.md. Locate them from this file —
harnesses load the same copy via symlink. Never hardcode a product home.

## 1 · Place the work first

- Cheap to verify (facts checkable, tests runnable, diffs reviewable) → workers.
- Verification costs as much as generation (taste, design, tricky concurrency) →
  a strong native agent instead. Wrong-once-unacceptable (hardware, destructive,
  live-state) → do it yourself. Free tilts this boundary; it never removes it.

## 2 · Decompose

- **scouts** (read-only, `"effort": "high"` for real review) for danger zones —
  concurrency, protocols, delivery pipelines. Demand CONFIRMED (traced path)
  vs PLAUSIBLE, with file:line + trigger + consequence.
- **workers** (edit) only with disjoint `owns` lists, only where every fix can
  state a concrete failure scenario. Ban refactors/style churn explicitly.
  Do not assign two workers the same browser, desktop, app, branch, or other
  mutable external system.
- Prompts are fully self-contained (workers see no conversation): goal, paths,
  authoritative facts, output shape, stopping point — and tell them **the
  working tree is truth; the brief is context**. Do not ask a worker to spawn
  more agents or run `codex`/`ocodex`. Never include secrets.
- Before launching, write the **ownership ledger**: files owned by you and by
  every live batch; the new `owns` lists must not collide with any of it.

## 3 · Launch

Write a master manifest (outside the repo when practical):

```json
{
  "workdir": "/absolute/path/to/project",
  "agents": [
    {
      "name": "trace-auth",
      "mode": "scout",
      "effort": "high",
      "task": "Find where login tokens are refreshed. Return file:line and likely failure points. Do not edit."
    },
    {
      "name": "fix-parser",
      "mode": "worker",
      "owns": ["src/parser.ts", "tests/parser.test.ts"],
      "after": ["trace-auth"],
      "task": "Fix the confirmed empty-input parser bug and add focused tests. Run only the relevant tests."
    }
  ]
}
```

```bash
OUT=<scratch>/ocodex-<name>; mkdir -p $OUT
python3 <this-skill>/scripts/launch_batches.py $OUT/master.json --out-dir $OUT  # background
python3 <this-skill>/scripts/fleet_watch.py $OUT                                # other pane, or --once
```

Launcher flags: `--out-dir` (required), `--max-workers N`, `--workers-per-key N`,
`--context-pack` (build and inject a commit-versioned CONTEXT.md), `--watch` /
`--no-watch` (live table on a tty; default on when stdout is a tty).

The launcher calls the sibling runner (`scripts/run_agents.py`); runner flags:
`--out-dir`, `--max-parallel N` (≤6), `--model SLUG`, `--timeout SECONDS`,
`--dry-run`. Pin a model slug only when asked — free-model availability varies.
The runner finds `ocodex` from `OCODEX_BIN`, PATH, `~/bin/ocodex`,
`~/.local/bin/ocodex` so desktop apps do not depend on shell startup files.

The launcher probes the key pool (orslot if present; ~5 workers/key, 1,000
req/key/day, ~40/agent), injects structured crash checkpoints (dead workers
leave `<name>.progress.md`), runs a work-stealing pool (one runner per agent;
the next job starts the moment a slot frees), retries a death once from the
checkpoint unless the failure is classified fatal, then writes `all.done`.
`"after": ["other-name"]` holds a job until those agents succeed — scouts
can stream into fixers in one launch. Two deaths of the same agent, or a
blocked dependency, go to the supervisor.

Watch a live batch (name, goal, state, runtime, last checkpoint, current
step) with `fleet_watch.py`, or `cat $OUT/status.txt`.

## 4 · Supervise

Spawn ONE supervisor immediately, pointed at the doctrine file — keep the
brief to the slots:

> Read SUPERVISOR.md (same directory as this SKILL.md) and follow it exactly.
> OUT=<out>. Repo: <path> (git baseline committed pre-launch). Workers and
> ownership: <list>. Concurrent work (expected in git status, off-limits):
> <ledger>. Real verification commands + expected results: <commands>.
> Authoritative facts: <facts>.

Harness: Claude Code — spawn a native supervisor (sonnet). Grok — spawn a
subagent. Codex/desktop — run SUPERVISOR.md yourself as its own step after
`all.done`. Never skip supervision.

Commit a git baseline BEFORE launching so every diff is worker output.

## 5 · Integrate

Act only on the supervisor's verdict. Final visual/hardware checks stay with
you — supervisors can build, not look. Report per-worker verdicts honestly,
including deaths and reverts.

## Known failure modes (measured)

Provider stream deaths (~25%; launcher retries once, then supervisor finishes
from the checkpoint) · Codex dropping the OpenRouter stream (proxy logs
`BrokenPipeError`; empty final file) · hung workers with no checkpoint
heartbeat (runner kills them after 600s stall) · confident factual errors
(verify every claim against source) · worker sandboxes cannot run real
builds (typecheck only — the supervisor runs real builds/tests) ·
supervisors judging foreign diffs (the ledger slot prevents it).
