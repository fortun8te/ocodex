# ocodex

**Free parallel coding agents you can actually trust.**

Cheap OpenRouter-backed workers (via an `ocodex` CLI) fan out on decomposable
work — doc sync, test authoring, bug hunts, mechanical fixes — while one paid
supervisor agent audits every claim, prunes every dubious fix, and finishes
the jobs of workers that die mid-run.

The economics are simple: **verification is cheaper than generation.** A fleet
of free-but-fallible workers plus one strict auditor beats a fleet of
expensive agents on any task whose output is cheap to verify.

## Field data (first day in production)

- 4 batches, 11 workers: ~25% died mid-run (provider stream errors)
- one "successful" docs batch shipped 8 confident factual errors
- the supervisor caught all 8 and finished 2 dead workers' tasks itself
- net errors shipped to the repo: **zero**

## Install

```bash
git clone https://github.com/fortun8te/ocodex && cd ocodex && ./install.sh
```

The script copies `SKILL.md`, `SUPERVISOR.md`, and the scripts into
`~/.claude/skills/ocodex` (and Grok / Codex / Cursor skill homes when those
directories exist).

Requirements:

- an `ocodex` CLI on PATH — Codex CLI pointed at OpenRouter, or any exec-style
  agent CLI that takes a prompt on stdin. If it lives somewhere non-standard,
  point `OCODEX_BIN` at it.
- optional: an `orslot` key-slot manager for multi-key pools (~5 concurrent
  workers per key). Without it, everything still works on a single key.

## Configure

| Env / flag | Default | Meaning |
|---|---|---|
| `OCODEX_BIN` | `ocodex` on PATH | worker CLI |
| `OCODEX_RUNNER` | bundled `run_agents.py` | batch runner |
| `ORSLOT_BIN` | `~/bin/orslot` | key-pool probe (optional; absent = single key) |
| `--workers-per-key` | 5 | concurrency per API key |
| `--max-workers` | pool-derived | hard concurrency ceiling |

## Use

Describe every agent in one manifest — scouts for read-only review, workers
for edits under explicit file ownership:

```json
{ "workdir": "/path/to/repo",
  "agents": [
    { "name": "find-bugs", "mode": "scout", "effort": "high",
      "task": "Read-only bug hunt in src/. Label CONFIRMED vs PLAUSIBLE, file:line, trigger, consequence." },
    { "name": "fix-parser", "mode": "worker", "owns": ["src/parser.ts"],
      "after": ["find-bugs"],
      "task": "Fix only bugs with a stated failure scenario. No refactors." } ] }
```

Launch:

```bash
python3 scripts/launch_batches.py manifest.json --out-dir ./out
python3 scripts/fleet_watch.py ./out          # other pane: name, goal, state, runtime, current step
```

The launcher probes your key pool, injects **crash checkpoints** into every
task (dead workers leave `<name>.progress.md` behind), runs a **work-stealing
pool** (next agent starts the moment a slot frees — no wave boundaries),
retries a death once from the checkpoint, and writes `all.done`.

`--context-pack` builds a commit-versioned `CONTEXT.md` and injects it into
every worker. `"after": ["other-name"]` waits for that agent to succeed
before spawning (scouts → fixers in one launch).

Live status is also `$OUT/status.txt` (`watch -n1 cat out/status.txt`).

Then spawn one supervisor agent (any strong model) with `SUPERVISOR.md` as
its doctrine, filling the slots: out-dir, file ownership, real verification
commands, authoritative facts.

## The three rules that make it work

1. **Never unsupervised.** Workers are the hands; the supervisor is the only
   quality gate.
2. **Disjoint ownership.** Every editing worker owns an explicit file list;
   nothing overlaps — not with other workers, not with you.
3. **Place by verifiability.** Cheap-to-verify → workers. Expensive-to-verify
   (taste, concurrency) → a strong agent. Wrong-once-unacceptable → yourself.

Full operating doctrine — placement rules, measured failure modes, the
supervisor template — in [`SKILL.md`](SKILL.md) (drops straight into Claude
Code as a skill).

## License

MIT
