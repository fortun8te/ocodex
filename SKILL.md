---
name: ocodex
description: The ocodex parallel-worker system — cheap external agents with mandatory paid supervision. Self-contained: doctor, run, status, wait_done, and ledger ship in scripts/. Use whenever a task splits into bounded chunks that are cheap to VERIFY (doc sync, test authoring, mechanical fixes, audits, parity chores, bug hunts), or whenever you'd spawn several subagents at once. Standing preference: reach for this before native subagents for routine parallel work.
---

# Ocodex, managed

Cheap OpenRouter-backed workers + one paid supervisor per batch. Never run
unsupervised. The harness retries empty/crash once, checkpoints as STEP ZERO
(with a heartbeat every 2 minutes), and writes ledger/stats/result files so
the supervisor does not reconstruct the run from prose.

Power is not "spawn more workers". Default ~6 concurrent per OpenRouter key.
`--workers-per-key 8` is allowed (429s hop/backoff, not a crash). Do not
default to 20. One supervisor is the quality gate. Scale with `orslot add`.

## The three rules

1. **Never unsupervised.** Workers are the hands; the supervisor is the only quality gate.
2. **Disjoint ownership.** Every editing worker owns an explicit file list; nothing overlaps.
3. **Place by verifiability.** Cheap-to-verify → workers. Expensive-to-verify (taste, concurrency) → a strong agent. Wrong-once-unacceptable → yourself.

## What you run (management layer)

Do **not** hand-write JSON, an ownership ledger, and a supervisor brief.
Call this, in order:

```bash
python3 ~/.claude/skills/ocodex/scripts/ocodex_managed.py doctor
python3 ~/.claude/skills/ocodex/scripts/ocodex_managed.py run $OUT/master.json --out-dir $OUT
# or:  ... run "Fix parser errors" --workdir /repo --owns src/parser.ts --out-dir $OUT
python3 ~/.claude/skills/ocodex/scripts/ocodex_managed.py status $OUT
```

`run` validates disjoint owns, writes `ledger.json`, prints pool headroom,
prints a filled SUPERVISOR brief, launches waves, waits on `all.done`.
Then spawn **one** supervisor with that printed brief
(`$OUT/supervisor-brief.md`). Watch the slot board instead of `sleep 60`.

Doctor must see SearXNG (`SEARXNG_URL`, default `http://127.0.0.1:8080`) and
Docker. Workers cannot web-search without it.

## Placement

- **Ocodex** when output is cheap to verify (facts vs source, tests, diffs).
- **Native agent** when verification costs as much as generation.
- **Main loop** when being wrong once is unacceptable.

## Field-measured failure modes (2026-08-25, four batches)

1. **~25% hard failure**: provider stream disconnects → empty output. Harness retries once; still empty → supervisor does the task.
2. **Confident factual errors**: one docs batch "succeeded" with 8 wrong claims. Workers are never the source of truth.
3. **Sandbox limits**: worker sandboxes cannot run the real build. Supervisor always does.
4. **Stale briefs**: the working tree is truth; the manifest is context.
5. **Cross-batch confusion**: every supervisor brief lists other live batches' owned files as expected-and-off-limits.

## Batch design

- Scouts (read-only, `effort: high` for real review): CONFIRMED vs PLAUSIBLE, file:line + trigger + consequence.
- Workers (edit): disjoint `owns`, each fix states a failure scenario. Ban refactors in the prompt.
- Worker prompts are a **compact brief** (goal, owns, facts, output contract, stop, STEP ZERO + heartbeat). Do **not** dump this file into the task string.
- The launcher rejects overlapping `owns` (exit 2) and refuses a stale out-dir that already contains `*.done` (exit 2).

## Checkpoints and heartbeats

STEP ZERO of every worker prompt: write analysis into `<OUT>/<name>.progress.md`
**before editing any owned file**. Then one checkpoint bullet per named
sub-task or per file touched. Append `HEARTBEAT <ISO-Z> | <subtask> | <focus>`
at least every **2 minutes** and after every sub-task. The slot board marks
STALE otherwise; the supervisor treats stale-without-result as likely-dead.
The harness creates the progress file at launch so a crash still leaves something.

## Supervision

Spawn prompt stays short — doctrine is SUPERVISOR.md; `run` already filled
the slots. Point the supervisor at `$OUT/supervisor-brief.md`. Parse
`<name>.result.json` before the prose final reply. Verify claims against
source. Revert anything you cannot confirm. Failed/empty/STALE worker →
finish from the checkpoint. Run the REAL build/tests yourself. Do not commit.

`status` is the live board (name, mode, focus, last update, elapsed,
heartbeat, alive/stale/dead/done, API/slot). No LLM required to render it.

## Bandwidth

Default `--workers-per-key 6` is rate-limit headroom, not a crash ceiling.
OpenRouter daily request limits reset every day — yesterday's 429s do not count today.
Request estimates in the launcher are labeled guesses, not measurements.

## Toward proper cloud agents (aspiration, not yet)

Workers still cannot message back mid-run. Until resumable sessions exist:
batches + supervisors is the honest ceiling.
