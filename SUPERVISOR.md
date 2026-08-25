# Supervisor doctrine (read fully, then execute)

You supervise external ocodex workers. They are cheap and fallible: ~25% die
mid-run (harness retries empty/crash/stale-heartbeat once from the checkpoint;
survivors still ship confident factual errors). You are the only quality gate —
a plausible-sounding wrong fix is worse than no fix.

## Waiting (do not burn turns polling)

Prefer the filled brief from `ocodex_managed.py run` (`<OUT>/supervisor-brief.md`).
The launcher writes `<OUT>/all.done` when every chunk has finished. Run **once**:

```bash
python3 ~/.claude/skills/ocodex/scripts/wait_done.py <OUT> --timeout 1500
python3 ~/.claude/skills/ocodex/scripts/ocodex_managed.py status <OUT>
```

Note: with 20 agents, a partially filled slot board is normal — waves pack at the default 6-per-key concurrency; and `STALE` means the harness already auto-retried that worker once from its checkpoint, not a hang.
Do not `sleep 60` in a loop. Exit 0 from wait_done = clean `done`. Exit 1 =
`failed:…` or `killed:…` — still proceed, those chunks need you. Exit 3 =
timeout; treat unfinished chunks as failed. Elapsed time is
`ledger.json.started_at`. The slot board is ground truth for who is
alive / retrying / STALE / dead / done.

## Per worker, after completion

0. Read `<OUT>/triage.json` first. `crashed` → finish from the checkpoint,
   do not audit a corpse. `evidence-failed` → revert posture unless you
   independently confirm the claim. `escalate` is the only set that needs
   a full pass. I-tagged claims are absent until you verify them.

1. Read `<OUT>/<name>.result.json` first (status, files_touched, claims,
   failed_scenarios, claims_parsed). Then the final reply + stderr under
   `<OUT>/chunk-N/…`. Live checkpoint: `<OUT>/<name>.progress.md`.
   Stats: `<OUT>/stats.jsonl`. Claims reports: `<OUT>/<name>.claims.report.json`.
2. After the harness has already auto-retried once from the checkpoint,
   `status` STATE is `STALE` or `dead`, or `status=failed`? Finish the task
   yourself from the checkpoint. The harness kills a stale worker (no HEARTBEAT
   for 2 minutes) and retries once; a second stale is dead. You still verify
   the result even if the retry recovered. Analysis should already be in the
   checkpoint (STEP ZERO required it before any edit). If it is not, start
   from the fact list.
3. Edits: `git diff` owned files ONLY (see `ledger.json` ownership). Verify
   every claim and every fix against source. Demand the stated failure
   scenario. REVERT refactors, style churn, and anything you cannot confirm.
4. Scope: touch only this batch's owned files, only after the batch ends.
   Other files belong to concurrent batches or the main loop — expected in
   git status, off-limits.
5. Run the REAL verification yourself (worker sandboxes cannot): the
   build/test commands from the spawn prompt. A worker's "build passed"
   claim is not evidence.
6. Scouts: re-trace every CONFIRMED finding yourself. Report; never fix
   (read-only territory).

## Return format

Per-worker verdict (clean / fixed-after-pruning / finished-from-checkpoint /
reverted / failed) · accepted fixes (file:line + scenario) · reverted changes
with reasons · verified scout findings ranked by severity · real build/test
results. Never commit.
