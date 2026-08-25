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
(`return 0 if summary["ok"] else 1`) and against this run's own
`chunk-0.done` / `all.done` contents.

## Not fixed — noted, not acted on

5. **The per-agent request estimate** is still a labeled guess. It now
   distinguishes scout vs worker and effort tier in `harness_lib.REQUEST_GUESS`
   so waves are less uniformly billed as 40, but the numbers were not
   measured. Low priority: getting it wrong just mis-sizes a wave.

## Done in the harness-power upgrade

6. **Feedback loop on worker death rate.** `run_agents.py` appends one JSON
   line per agent to `{out-dir}/stats.jsonl` and, if set, `$OCODEX_STATS`
   (e.g. `~/.ocodex/stats.jsonl` for cross-batch history). Fields: name,
   effort, model, ok, return_code, seconds, error_class, retries. The
   harness retries empty output / stream-fail / crash **once**, then
   leaves it for the supervisor. Both attempts are recorded.

7. **Checkpoint granularity is per sub-task.** The injected CHECKPOINT
   clause is STEP ZERO of the task (not an addendum). It requires one
   append per named sub-task or per file touched, not one vague session
   bullet.

8. **Upfront analysis is required before edits.** STEP ZERO tells the
   worker to write analysis into the checkpoint BEFORE editing any owned
   file. The harness also creates `<OUT>/<name>.progress.md` at launch
   so a crash still leaves a file.

9. **Launcher process-management is tested.** `tests/` spawns
   `launch_batches.py` against a stub `OCODEX_BIN`, SIGTERMs mid-run,
   and asserts children are gone and `chunk-*.done` exists. Also covers
   stale-marker refusal, `all.done` failure signaling, disjoint-owns
   rejection, checkpoint language, and retry-once.

10. **Elapsed time is on disk.** `ledger.json` is written at launch with
    `started_at`, ownership, and chunk plan, and updated as chunks
    finish. `scripts/wait_done.py` prints elapsed seconds from that
    stamp. Supervisor doctrine no longer treats 25 minutes as memory.

Also shipped in that upgrade (beyond 6–10): compact worker briefs (no
SKILL/SUPERVISOR dump into the prompt), `<name>.result.json` contract so
the supervisor parses instead of skimming prose, launch-time disjoint
`owns` validation, `scripts/ocodex_managed.py`, README clone URL fixed
to `fortun8te/ocodex`, and `install.sh` copies every `scripts/*.py`.

## What this worker found awkward about its own harness

- The CHECKPOINT clause used to be appended as an addendum, competing
  with the task. That is now STEP ZERO, with analysis-before-edit
  required. Kept here so the original failure mode stays documented.
- Prompts have to be self-contained because there is no mid-task
  check-in. Compact briefs + a result.json footer are the mitigation;
  workers still cannot ask a clarifying question.
- The sandbox can compile but used to be unable to exercise process
  isolation. Item 9's tests close that gap for the launcher paths.

Also in this upgrade (management + observability):

- `ocodex_managed.py` is the UX: `doctor`, `run` (goal or manifest), `status`,
  `wait`. `run` prints a filled SUPERVISOR brief so the skill user does not
  hand-write JSON + ledger + spawn prompt.
- Live slot board (`status` / `scripts/ocodex-status`) reads ledger +
  progress + result.json + stats.jsonl. No LLM.
- Mandatory HEARTBEAT every 2 minutes in the checkpoint; `status` marks
  STALE; supervisor treats stale-without-result as likely-dead.
- `./install.sh` is a real doctor: ocodex, optional orslot, docker, SearXNG
  (`SEARXNG_URL`, default http://127.0.0.1:8080). SearXNG is a real install
  dependency for worker web search.
- Default `--workers-per-key 6` is rate-limit headroom, not a crash. 8 is
  allowed; 429s hop/backoff. Do not default to 20. OpenRouter daily request limits reset every day.

## Shipped after the harness-power upgrade

11. **Live cap + dry-pool defer.** `CapacityMonitor` re-probes orslot during
    the run (`OCODEX_CAP_REFRESH_SEC`, default 30s). Remaining 0 → stop
    launching, mark leftover agents `deferred` in `all.done` / ledger.
12. **Work-stealing pool.** One runner per agent. A finished worker frees a
    slot for the next agent instead of waiting on a 6-agent wave.
13. **OpenRouter `/key` snapshot** into `ledger.json` when `OPENROUTER_API_KEY`
    is in the environment. Still no req/day from their API.
14. **Fatal vs transient retry.** `classify.py` skips retry on invalid key /
    sandbox / missing binary. Stream/429/empty still retry once; retry may
    hop to the next model in the provider tier chain.
15. **CLAIM schema + triage.json.** Workers must emit `CLAIM | C|I | …`
    lines. `triage.py` runs after `all.done`; supervisor reads it first.
16. **BYOK providers.json.** Groq (and any OpenAI-compatible endpoint) via
    `~/.ocodex/providers.json`. Scouts: `OCODEX_SCOUT_PROVIDER=groq`.
