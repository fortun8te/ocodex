# SUGGESTIONS.md — improvement roadmap

Written by the supervisor finishing the `self-improve` worker's task after it
crashed (transient OpenRouter/codex stream disconnect) partway through, right
after landing its `scripts/launch_batches.py` fixes and before writing this
file. Its checkpoint (`self-improve.progress.md`) already contained a full
traced analysis of six candidate bugs; that analysis is folded in below
rather than redone. Item 1 was independently reproduced live in the very
batch that killed this worker, before any fix existed — see below.

## Fixed this batch (in `scripts/launch_batches.py`)

1. **`all.done` hid chunk failures.** The launcher wrote a bare `"done"`
   after the wait loop regardless of any chunk runner's exit code, so a
   supervisor watching `all.done` had no signal that a chunk had crashed.
   This is not hypothetical: it happened in the exact batch that produced
   this file. `self-improve` crashed, `run_agents.py` exited 1 for that
   chunk, `chunk-0.done` correctly recorded `1` — and `all.done` still said
   `done`. Fixed: the launcher now tracks failed chunk indices, writes
   `failed:N,M` into `all.done` when any chunk fails, prints a `FAILED`
   line per chunk, and exits 1. The supervisor still wakes on the same
   file (no polling-loop change needed) but can no longer mistake a
   failure for a clean run without opening a single log.

2. **Stale `--out-dir` reuse.** Rerunning against an out-dir from a
   previous batch left old `all.done` / `chunk-N.done` markers sitting
   there; a supervisor could start watching, see the leftover `all.done`
   from the *last* run, and declare victory before the new run had done
   anything. Fixed: the launcher now refuses to start if any `*.done`
   file already exists in the out-dir (exit 2, names the offending
   files). Cheap guard, no silent data loss.

3. **Orphaned runner processes on Ctrl-C / kill.** The launcher spawned
   `run_agents.py` children with no process-group isolation and no
   signal handlers. Killing the launcher left runner processes (and
   whatever `ocodex` subprocesses they'd spawned) alive, still holding
   provider slots and still writing into the out-dir. Fixed:
   `start_new_session=True` on each child plus `SIGINT`/`SIGTERM`
   handlers that `killpg` every still-running child (`SIGTERM`, then
   `SIGKILL` after a 5s grace period) before the launcher exits.

4. **Orslot regex assumed trailing whitespace after `used/cap`.** The
   original pattern required `\s` right after the `N/M` pair, so a line
   ending exactly at `cap` with no trailing character would fail to
   match, silently falling back to the `(1, 1000)` default and
   under-provisioning concurrency. Verified this doesn't regress the
   real `orslot` output on this machine (still parses identically);
   the failure case itself is defensive, not reproduced against a real
   drifted format — flagging that distinction rather than overclaiming
   it as a confirmed live bug the way items 1–3 are.

All four verified with `python3 -m py_compile scripts/launch_batches.py
scripts/run_agents.py` (passes) and, for items 1–3, by re-tracing the
diff against `scripts/run_agents.py`'s actual exit-code contract
(`return 0 if summary["ok"] else 1`, line 349) and against this run's own
`chunk-0.done` / `all.done` contents.

## Not fixed — noted, not acted on

5. **The ~40-requests-per-agent estimate** (`scripts/launch_batches.py`,
   used for capacity hints; sourced from SKILL.md's "~40 requests is a
   sane per-agent budget") is a labeled guess, not a bug. It doesn't
   distinguish scout effort from worker effort, so it over-estimates
   scout cost and under-estimates a `high`-effort worker's. Low
   priority: getting it wrong just mis-sizes a wave, it doesn't corrupt
   state. Worth revisiting only if capacity planning becomes a real
   pain point.

## Roadmap — not yet attempted, ranked by expected impact

6. **No feedback loop on worker death rate.** ~25% of workers die, and
   that number is treated as an accepted constant rather than
   something the system learns from. This very batch is a (n=1, so
   treat as anecdote, not evidence) data point where the `effort: high`
   worker (`self-improve`) died and the default-effort worker
   (`polish-and-market`) didn't — plausibly just noise, but currently
   unknowable either way because nothing records it. Sketch: have
   `run_agents.py` append one JSON line per agent
   (`{name, effort, model, ok, return_code, seconds}`) to a persistent
   `stats.jsonl` at the repo root (not the ephemeral out-dir), so
   patterns across many batches — by effort tier, by task shape, by
   time of day — become visible instead of anecdotal.

7. **Checkpoint granularity is per work-session, not per sub-task.**
   This worker's own checkpoint proves the problem: after implementing
   four separate, independently-traceable fixes (A–D), it wrote one
   checkpoint bullet — "fixes implemented and verified" — covering all
   four, then died. Finishing from that checkpoint required re-reading
   the whole diff to figure out which fix was which, instead of the
   checkpoint just saying so. Sketch: the injected CHECKPOINT clause
   should ask for one append per named sub-task or per file touched,
   not one append per "session" of work — cheap to ask for, and it's
   the difference between a supervisor resuming in seconds versus
   doing diff archaeology.

8. **Upfront analysis is one crash away from being lost.** This
   worker's task packed in six candidates to "examine hard" before
   touching any code. It happened to park its full A–G reasoning in
   the checkpoint *before* starting edits — which is exactly why this
   file could be finished from checkpoint alone instead of needing a
   re-read of the source. That was worker discipline, not something
   the harness required. Sketch: make "write your analysis into the
   checkpoint before editing any owned file" an explicit numbered step
   in the injected CHECKPOINT clause (SKILL.md / `launch_batches.py`'s
   task-string injection), not left to individual workers to think of.

9. **Nothing exercises the launcher's process-management paths.** The
   signal handling, `killpg` behavior, and stale-marker refusal added
   this batch are only verified by reading the diff and by
   `py_compile` — nothing actually spawns a child, sends it a signal,
   and asserts no orphan remains, because that's outside what either a
   worker's sandbox or a supervisor should improvise ad hoc mid-review.
   Sketch: a small `tests/` smoke test — spawn `launch_batches.py`
   against a trivial one-agent manifest, `SIGTERM` it mid-run, assert
   the child process is gone and `chunk-0.done` exists — would let
   future changes to this file be verified mechanically instead of by
   supervisor code-reading alone.

10. **Supervisor's 25-minute give-up threshold is memory, not state.**
    SUPERVISOR.md says "give up after ~25 min," but that's tracked only
    in the supervisor's own background-wait duration, not recorded
    anywhere a second supervisor (or a watchdog) could check
    independently. Sketch: have the launcher stamp a start timestamp
    into `ledger.json` when it begins, so elapsed time is a fact on
    disk rather than something inferred from wall-clock context.

## What this worker found awkward about its own harness

- The CHECKPOINT clause is appended to the task prompt as an addendum,
  competing for attention with the actual task rather than being
  step zero. It worked here because this worker chose to front-load
  its analysis into the checkpoint before editing — but that was a
  judgment call the harness didn't force, and a differently-ordered
  worker could just as easily have started editing first and died with
  nothing durable but a partial diff.
- The prompt has to be fully self-contained because there is no
  mid-task check-in: a worker gets one shot to carry six named
  investigation targets, a fix policy ("surgical fixes only, comments
  in the file's voice"), a verification command, and a deliverable
  spec, all without any ability to ask a clarifying question if the
  candidates turn out to be non-bugs (see item 5 above — the worker had
  to decide unilaterally that the ~40-estimate wasn't a confirmed bug
  and just say so, with no way to check that judgment against anyone).
- The sandbox can compile but can't fully exercise what it changed:
  `python3 -m py_compile` proves the file parses, not that
  `start_new_session=True` plus the signal handlers actually prevent an
  orphaned process. That gap is real and is item 9 above.
