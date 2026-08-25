---
name: ocodex
description: The ocodex parallel-worker system — free external agents with mandatory Sonnet supervision. THE single entry point for ocodex (self-contained: the runner ships in scripts/). Use whenever a task splits into bounded, self-contained chunks that are cheap to VERIFY (doc sync, test authoring, mechanical fixes, audits, parity chores, bug hunts), or whenever you'd spawn several subagents at once. Standing preference: reach for this before native subagents for routine parallel work; scale bandwidth with concurrent batches.
---

# Ocodex, managed — one skill

Free external workers + one paid Sonnet auditor per batch. The composite has
shipped zero errors; the raw workers alone would have shipped plenty. Never
run unsupervised.

## The placement rule (decides everything)

- **Ocodex** when the output is *cheap to verify* (facts checkable against
  source, tests runnable, diffs reviewable).
- **Native agent (opus/fable)** when verification costs as much as generation
  (taste, design, tricky concurrency, anything the owner rejected drafts of).
- **Main loop** when being wrong once is unacceptable (hardware, destructive
  ops, live-state debugging).
Free tilts this boundary; it does not remove it.

## Field-measured failure modes (2026-08-25, four batches)

1. **~25% hard failure**: provider stream disconnects → empty output. Retry
   once; still empty → the supervisor does the task itself.
2. **Confident factual errors**: one docs batch "succeeded" with 8 wrong
   claims. Workers never get to be the source of truth — every fact goes in
   the prompt or gets verified against code.
3. **Sandbox limits**: worker sandboxes block SwiftPM/clang module-cache
   writes. Tell Swift workers to verify with `swiftc -typecheck` or
   best-effort build; the SUPERVISOR always runs the real build/tests.
4. **Stale briefs**: the tree may have moved since the manifest was written.
   Tell workers: *the working tree is truth; the manifest is context* — one
   worker's best decision was deviating from a stale brief correctly.
5. **Cross-batch confusion**: a supervisor seeing another batch's diffs will
   try to judge them. Every supervisor brief MUST list the other live
   batches' owned files as expected-and-off-limits.

## Batch design

- Scouts (read-only, `effort: high` for real review) for danger zones — BLE,
  concurrency, delivery pipelines. Demand CONFIRMED (traced path) vs
  PLAUSIBLE labels with file:line + trigger + consequence.
- Workers (edit) only with disjoint `owns` lists and only where each fix can
  state a concrete failure scenario. Ban refactors and style churn in the
  prompt, explicitly.
- ≤6 agents per batch (runner cap), ≤1 build-heavy worker per batch.
- Prompts fully self-contained: goal, paths, authoritative facts, output
  shape, stopping point. Workers see no conversation.

## Bandwidth: the slot pool is the ceiling

Every ocodex worker process claims its OWN OpenRouter key slot (`orslot
claim`), and the counting proxy retries 429s on a different slot — multi-key
parallelism is native. Rules of thumb: ~5 concurrent workers per key, 1,000
requests/key/day (~40/agent budget). Grow bandwidth with `orslot add`; check
headroom with `orslot` (instant) before sizing a batch.

Use the capacity-aware launcher for anything beyond a handful of agents —
one master manifest, any number of agents; it probes the pool, injects crash
checkpoints into every task, chunks into ≤5-agent runner waves under the
concurrency cap, and writes `all.done` when finished:

```bash
OUT=<scratchpad>/ocodex-<name>; mkdir -p $OUT
# write $OUT/master.json ({"workdir": ..., "agents": [{name, mode, owns?, effort?, task}]})
python3 ~/.claude/skills/ocodex-managed/scripts/launch_batches.py $OUT/master.json --out-dir $OUT   # run_in_background
```

Before launching, write the **ownership ledger** — files owned by the main
loop and each live batch — and check the new `owns` lists against all of it.
SwiftPM lock contention between chunks waits; it does not corrupt.

## Crash auto-save

The launcher appends a CHECKPOINT clause to every task: workers must create
`<OUT>/<name>.progress.md` immediately and append after each step. When a
worker dies (~25% do), the supervisor finishes the task from that file
instead of starting over. Hand-written single batches via the legacy runner
should include the same clause manually.

## Supervision

Immediately spawn one Sonnet supervisor per launch (Agent tool). Keep the
spawn prompt SHORT — point it at the doctrine file and fill only the slots:

> Read ~/.claude/skills/ocodex-managed/SUPERVISOR.md and follow
> it exactly. OUT=<out-dir>. Repo: <path>. Workers and ownership: <list>.
> Concurrent work (expected in git status, off-limits): <ledger>. Real
> verification commands + expected results: <commands>. Facts that are
> authoritative: <facts>.

Do not pre-review worker output yourself; integrate only after its report.
The final visual/hardware check (a UI on screen, a device behaving) stays
with the main loop — supervisors can build, not look.

## Supervisor brief template (fill every <slot>)

> You supervise ocodex workers whose batch runs under <OUT> (unique batch
> subfolder: per-agent final replies + stderr; manifest.json has the tasks).
> Poll with `sleep 60` loops until finished (~20 min cap; runner timeout 15).
> CONCURRENT WORK: <other live batches + main loop file ownership — expected
> in git status, off-limits to you>. Then, per worker: read final reply and
> stderr; `git diff` its owned files; verify every claim and every fix
> against the actual source — a plausible-sounding wrong fix is worse than no
> fix; REVERT refactors, style churn, and fixes whose scenario you cannot
> confirm by reading the code. Scope: edit only inside this batch's owned
> files, only after the batch ends. Failed/empty worker → do its task
> yourself from the fact list. Run the REAL verification yourself:
> <build/test commands + expected results — worker sandboxes cannot>.
> Scout findings: re-trace CONFIRMED ones yourself; report, never fix them.
> Do not commit. Return: per-worker verdict (clean / fixed-after-pruning /
> reverted / failed), accepted fixes (file:line + scenario), reverted changes
> with reasons, verified findings ranked, final build+test results.

## Toward proper cloud agents (aspiration, not yet)

What blocks it today: workers can't message back mid-run, share no context,
sit in write-limited sandboxes, and free-slot rate limits make them flaky
(~25%). When ocodex gains resumable sessions or live messaging, promote this
pattern: persistent named workers, supervisor as router, main loop as owner.
Until then: batches + supervisors is the honest ceiling.
