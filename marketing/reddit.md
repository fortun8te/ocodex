# Reddit post — two variants

---

## Variant 1: r/LocalLLaMA

**Title:**

I run free OpenRouter models as parallel coding agents and a paid supervisor catches everything they get wrong. First-day numbers inside.

**Body:**

Free models are great until you actually let them touch your codebase. I wanted to find out how far they go with the right harness around them, so I built one and ran it for a day.

Setup: several `ocodex` workers (Codex CLI pointed at free OpenRouter slots) running in parallel on decomposable tasks — bug hunts, doc sync, mechanical fixes. One paid supervisor model audits everything afterward.

First day in production, 4 batches, 11 workers:

- ~25% of workers died mid-run from provider stream errors
- one batch that reported success shipped 8 confident factual errors
- supervisor caught all 8 and finished 2 dead workers' tasks itself
- net errors shipped to the repo: zero

The interesting part isn't the crash rate — it's that crashes aren't the dangerous failure mode. Dead workers leave checkpoint files and are easy to finish by hand. The dangerous mode is a survivor confidently shipping something plausible-but-wrong that looks like clean output. So every worker diff gets read against actual source before it's accepted, refactors get reverted on sight, and scout findings get re-traced before being believed.

The economics: verification is cheaper than generation. Free workers do the typing; one strong model does the checking; the composite ships zero errors where either alone would ship some.

Tooling is two Python scripts:

- `run_agents.py` — runs up to 6 agents per batch, enforces non-overlapping file ownership between editing workers, supports dry-run.
- `launch_batches.py` — takes one manifest with any number of agents, probes your key pool (optional orslot multi-key), injects crash checkpoints into every task, chunks into waves under the concurrency cap.

Repo with full docs and the measured failure modes:
https://github.com/fortun8te/ocodex-managed

Happy to share more details about which tasks worked well (bug hunts and doc sync were great) and which didn't (anything needing real build/test loops — worker sandboxes can't run them).

---

## Variant 2: r/ClaudeCode / r/ClaudeAI

**Title:**

I built a Claude Code skill that gives me a fleet of free OpenRouter workers plus a Sonnet supervisor that audits everything. Zero errors shipped on day one.

**Body:**

Claude Code subagents cost tokens. Sometimes you just need ten things checked or fixed at once and don't care who does the typing. This skill points cheap free OpenRouter agents at your repo instead, then uses one paid Claude supervisor to make sure nothing bad survives.

Repo (installs as a skill via install.sh):
https://github.com/fortun8te/ocodex-managed

How it fits into a Claude Code workflow:

1. You write one JSON manifest describing any number of agents — read-only scouts for bug hunts, editing workers with explicit file ownership lists.
2. `scripts/launch_batches.py` probes your OpenRouter key pool, chunks agents into waves under the concurrency cap (~5 workers per key), injects a crash-checkpoint clause so dead workers leave `<name>.progress.md` behind, and writes an all.done file when finished.
3. You spawn one Claude supervisor per batch with SUPERVISOR.md as its doctrine. It waits on all.done, reads each worker's final reply, diffs only the owned files, verifies every fix against actual source, reverts anything it can't confirm, finishes crashed jobs from checkpoints, and runs the real build/tests itself (worker sandboxes can't).

Real first-day numbers: 4 batches, 11 workers, ~25% died mid-run from provider stream errors, one "successful" batch shipped 8 confident factual errors, supervisor caught all 8 and finished 2 dead workers' tasks itself. Net errors to the repo: zero.

The three rules baked into the doctrine:

- Never unsupervised — workers are hands, the Claude supervisor is the only quality gate.
- Disjoint ownership — the runner refuses overlapping file lists between editing workers.
- Place by verifiability — cheap-to-verify goes to free workers; taste/concurrency stays with the strong agent.

It's not trying to replace native subagents for everything — it's for when work decomposes into bounded self-contained chunks that are cheap to verify: doc syncs, test authoring, parity chores, mechanical fixes, parallel bug hunts. For those, the token bill drops to roughly zero and quality holds because the supervisor reads every diff.

SKILL.md has the full operating doctrine including measured failure modes (confident wrong answers beat crashes as the top threat). Would love feedback on the supervision loop design.
