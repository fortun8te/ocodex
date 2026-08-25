# Show HN: ocodex — free OpenRouter workers with a paid supervisor that audits everything

## Title

Show HN: I run free OpenRouter agents in parallel and one paid supervisor audits everything they do

## Body

I've been running coding tasks on free OpenRouter models through the `ocodex` CLI, several workers at a time, and treating them like cheap labor that can't be trusted unsupervised. The repo is the pattern plus the tooling:

https://github.com/fortun8te/ocodex

First day of production data, four batches, eleven workers:

- ~25% of workers died mid-run from provider stream errors
- one batch that reported "success" shipped 8 confident factual errors in docs
- the supervisor caught all 8 and finished 2 dead workers' tasks itself
- net errors that made it into the repo: zero

That's the whole pitch: verification is cheaper than generation. The workers are free and fallible; the supervisor is a strong model whose only job is to check every claim against source, revert anything it can't confirm by reading the code, and finish crashed jobs from checkpoint files.

How it works:

1. You write one JSON manifest describing any number of agents — read-only scouts for bug hunts, editing workers with explicit file ownership.
2. A launcher probes your key pool (optional multi-key via orslot), injects a crash-checkpoint clause into every task, chunks agents into waves under the concurrency cap, and runs them.
3. Dead workers leave `<name>.progress.md` behind. A quarter of them will die; that's expected, not exceptional.
4. You spawn one supervisor (any strong model) with SUPERVISOR.md as doctrine. It waits on an all.done file, then per worker: reads the final reply, diffs the owned files, verifies every fix against actual source, reverts refactors and unconfirmable fixes, runs the real build/tests (worker sandboxes can't), and re-traces scout findings before ranking them.

Three rules hold the system together:

- Never unsupervised. Workers are hands, not judgment.
- Disjoint ownership. Editing workers get explicit file lists; nothing overlaps, enforced by the runner's validation.
- Place by verifiability. Cheap-to-verify work goes to workers; expensive-to-verify (taste, concurrency) goes to a strong agent; wrong-once-unacceptable stays human.

What I measured that surprised me most wasn't the crash rate — it was that the failure mode isn't crashes, it's confident wrong answers from survivors. Crashes leave checkpoints; confident errors look like clean output. That's why the supervisor reads every diff against source instead of trusting "done" messages.

The runner is capacity-aware (~5 concurrent per OpenRouter key), enforces non-overlapping ownership inside the workdir, supports per-agent timeout/model/effort, and has a dry-run mode. The launcher handles arbitrary agent counts via chunking. `install.sh` doctors the install (ocodex, docker, SearXNG) and drops it into Claude Code as a skill.

Happy to answer questions about the failure modes, the supervision loop, or where this pattern breaks down.
