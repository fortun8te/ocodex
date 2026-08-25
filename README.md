# ocodex

A parallel cheap-worker harness: OpenRouter-backed `ocodex` CLI workers fan out
on decomposable, cheap-to-verify work; one paid supervisor audits every claim,
prunes bad fixes, and finishes dead workers. Verification is cheaper than
generation — that is the whole product.

Workers are fallible (stream disconnects, confident factual errors). They never
ship unsupervised. The launcher is capacity-aware, crash-checkpoints every
task, heartbeats every 2 minutes, retries empty/crash once, and leaves a
machine-readable ledger so the supervisor does not burn tokens reconstructing
what happened.

Power is **not** "spawn more workers". Default ceiling is ~6 concurrent
workers per OpenRouter key. Scale with more keys (`orslot add`) and better
packing. `--workers-per-key 8` is allowed (429s backoff/hop). Do not default
to 20. One supervisor remains the quality gate. OpenRouter daily limits reset every day.

## Why a supervisor exists

First day in production (2026-08-25): 4 batches, 11 workers. About 25% died
mid-run on provider stream errors. One "successful" docs batch shipped 8
confident factual errors. The supervisor caught all 8 and finished 2 dead
workers' tasks. Net errors shipped: **zero**.

That is why the quality gate is a strong model reading diffs and running real
builds — not the worker's own "done" claim.

## Install

```bash
git clone https://github.com/fortun8te/ocodex && cd ocodex && ./install.sh
```

`./install.sh` is the whole setup path. It:

1. Copies `SKILL.md`, `SUPERVISOR.md`, `README.md`, and `scripts/` into
   `~/.claude/skills/ocodex` (and `~/.codex/skills/ocodex` when `~/.codex`
   exists).
2. Writes `examples/sample-manifest.json` with an absolute workdir.
3. Runs **doctor**: `ocodex` on PATH, optional `orslot`, Docker, SearXNG.

Re-run checks anytime:

```bash
python3 scripts/ocodex_managed.py doctor
```

### SearXNG (required for worker web search)

Someone else installing this **must** run SearXNG with Docker. Workers cannot
web-search without it. Doctor fails with the next command if it is down.

```bash
docker compose -f examples/searxng-compose.yml up -d
export SEARXNG_URL=http://127.0.0.1:8080   # default
python3 scripts/ocodex_managed.py doctor
```

Equivalent one-liner:

```bash
docker run --name searxng -d -p 8080:8080 searxng/searxng:latest
```

### Other requirements

- An `ocodex` CLI on PATH (Codex CLI pointed at OpenRouter, or any exec-style
  agent that takes a prompt on stdin). Non-standard location: `OCODEX_BIN`.
- Optional: `orslot` for multi-key pools. Without it, one key, default 6
  concurrent. 429s hop/backoff; they are not a crash at 7+ workers.

## First sample launch

After doctor is clean:

```bash
python3 scripts/ocodex_managed.py run examples/sample-manifest.json --out-dir /tmp/ocodex-sample
python3 scripts/ocodex_managed.py status /tmp/ocodex-sample
# or: scripts/ocodex-status /tmp/ocodex-sample
```

Then spawn **one** supervisor with the printed SUPERVISOR BRIEF
(`/tmp/ocodex-sample/supervisor-brief.md`).

## Configure

| Env / flag | Default | Meaning |
|---|---|---|
| `OCODEX_BIN` | `ocodex` on PATH | worker CLI |
| `OCODEX_RUNNER` | bundled `scripts/run_agents.py` | per-chunk runner |
| `ORSLOT_BIN` | `~/bin/orslot` | key-pool probe (optional; absent = one key) |
| `SEARXNG_URL` | `http://127.0.0.1:8080` | SearXNG; doctor fails if unreachable |
| `OCODEX_STATS` | `{out-dir}/stats.jsonl` | extra JSONL stats sink (e.g. `~/.ocodex/stats.jsonl`) |
| `OCODEX_BATCH_OUT` | set by launcher | batch out-dir (checkpoints + result.json) |
| `OCODEX_KEY_SLOT` | unset | recorded on the slot board when known |
| `OCODEX_POLL_INTERVAL` | `5` | launcher wait-loop seconds |
| `--workers-per-key` | **6** | default concurrency per key; override 8–10, not 20 |
| `--max-workers` | pool-derived | hard cap for this run; exceeding recommended 6/key warns, does not crash |
| `--timeout` | 900 | per-agent seconds |

## Management CLI

This is the whole UX. Do not hand-write JSON + ledger + a supervisor brief
unless you want to.

```bash
python3 scripts/ocodex_managed.py doctor
python3 scripts/ocodex_managed.py run <manifest.json|goal> --out-dir OUT [--owns path] [--workdir DIR]
python3 scripts/ocodex_managed.py status OUT          # live slot board, no LLM
python3 scripts/ocodex_managed.py wait --out-dir OUT  # blocks on all.done
```

`run` with a short goal generates the manifest:

```bash
python3 scripts/ocodex_managed.py run "Fix only parser errors with a failure scenario" \
  --workdir /path/to/repo --owns src/parser.ts --out-dir ./out/parser
```

It validates disjoint `owns`, writes `ledger.json`, prints pool headroom,
prints a filled SUPERVISOR brief, launches waves, waits, and can
`--watch-status` the slot board while workers run.

## Slot board

`status` prints every agent in the batch from disk (ledger + progress +
result.json + stats.jsonl). No LLM.

| Column | Source |
|---|---|
| NAME / MODE | ledger |
| FOCUS | last heartbeat one-liner, else task title |
| SUBTASK | last named heartbeat sub-task |
| LAST UPDATE | last checkpoint line |
| ELAPSED | now − `ledger.json` `started_at` |
| HEARTBEAT / AGE | last `HEARTBEAT <ISO-Z> \| subtask \| focus` line |
| STATE | `alive` / `retrying` / `STALE` / `dead` / `done` |
| API/SLOT | model + key slot if recorded |

Heartbeat older than **2 minutes** without a result → `STALE`. Supervisor
treats stale-without-result as likely-dead.

## The three rules

1. **Never unsupervised.** Workers are the hands; the supervisor is the only
   quality gate.
2. **Disjoint ownership.** Every editing worker owns an explicit file list;
   nothing overlaps — not with other workers, not with you. The launcher
   rejects overlap (exit 2).
3. **Place by verifiability.** Cheap-to-verify → workers. Expensive-to-verify
   (taste, concurrency) → a strong agent. Wrong-once-unacceptable → yourself.

## Manifest (when you do write one)

```json
{
  "workdir": "/path/to/repo",
  "agents": [
    {
      "name": "find-bugs",
      "mode": "scout",
      "effort": "high",
      "task": "Read-only bug hunt in src/. Label CONFIRMED vs PLAUSIBLE with file:line, trigger, consequence."
    },
    {
      "name": "fix-parser",
      "mode": "worker",
      "owns": ["src/parser.ts"],
      "facts": ["Parser errors must keep the existing error-code table."],
      "task": "Fix only bugs with a stated failure scenario. No refactors."
    }
  ]
}
```

Optional per-agent: `effort` (`low|medium|high|xhigh`), `model`, `timeout`
(min 30), `facts` (string or list). Worker prompts are a **compact brief**
(goal, owns, facts, output contract, stop, STEP ZERO checkpoint). Doctrine
is not dumped into the task string.

If `$OUT` already contains `*.done` markers, the launcher **refuses** (exit
2). Use a fresh out-dir.

## What a run writes

```
$OUT/
  ledger.json              start ts, ownership, chunk plan, headroom; updated as chunks finish
  stats.jsonl              one JSON line per agent (ok, return_code, seconds, retries, error_class, slot)
  all.done                 "done" or "failed:0,2" or "killed:15"
  supervisor-brief.md      filled spawn prompt
  chunk-N.manifest.json
  chunk-N.runner.log
  chunk-N.done             runner exit code
  chunk-N/                 per-agent final replies + stderr
  <name>.progress.md       checkpoint + HEARTBEAT lines (created at launch)
  <name>.result.json       status, files_touched, claims, failed_scenarios
```

## Scripts

| Path | Role |
|---|---|
| `scripts/ocodex_managed.py` | `doctor` `run` `launch` `status` `wait` |
| `scripts/ocodex-status` | thin alias → `status` |
| `scripts/launch_batches.py` | waves, ledger, checkpoints, signals, headroom |
| `scripts/run_agents.py` | compact brief, retry-once, stats, result.json |
| `scripts/wait_done.py` | block until `all.done` (no LLM sleep loop) |
| `scripts/harness_lib.py` | validation, checkpoint text, stats, status board |
| `examples/sample-manifest.json` | first-run scout |
| `examples/searxng-compose.yml` | SearXNG via Docker |

## Troubleshooting

**Dead / empty workers (~25% on stream errors).** Harness retries **once**
on `return_code != 0` or empty final reply, then leaves it. `status` shows
`retrying` then `dead`. Supervisor finishes from `<name>.progress.md`.

**STALE on the slot board.** Last heartbeat older than 2 minutes and no
result. Treat as likely-dead.

**`refusing to run: all.done already exist` (exit 2).** Stale out-dir. New
`--out-dir` or delete `*.done`.

**`ownership overlap` (exit 2).** Two editing workers claimed the same path
(or a parent/child pair). Split `owns`.

**`all.done` says `failed:N`.** That chunk's runner exited non-zero. Open
`chunk-N.done`, `chunk-N.runner.log`, `stats.jsonl`. Existence of `all.done`
is not success — read its contents.

**Ctrl-C / SIGTERM.** Launcher `killpg`s each runner; runners kill `ocodex`
children; `chunk-N.done` / `all.done` are written on the way down.

**doctor: MISS searxng.** Start it with the compose file above. Workers
cannot web-search without it.

**429 at 7+ workers.** Not a crash. Default is 6/key for rate-limit headroom.
`--workers-per-key 8` is allowed; the counting proxy retries/hops slots.

## Tests

No live API keys. Stub `OCODEX_BIN` with `tests/fake_ocodex.py`:

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
