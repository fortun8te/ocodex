# X thread — 6 posts

## 1/6

Ran free OpenRouter models as a fleet of parallel coding agents for one day. 11 workers, ~25% died mid-run, one "successful" batch shipped 8 confident factual errors. Net errors that reached my repo: zero.

Here's the system and what it taught me about verification economics:

## 2/6

The setup: several ocodex workers (Codex CLI on free OpenRouter slots) fan out on decomposable tasks — bug hunts, doc syncs, mechanical fixes. Each editing worker gets an explicit file-ownership list; the runner refuses overlaps. One paid supervisor model audits everything afterward.

## 3/6

Crashes weren't the dangerous part. Dead workers leave checkpoint files behind and are easy to finish by hand. The dangerous failure mode is a survivor confidently shipping something plausible-but-wrong. That batch looked completely clean from its output.

## 4/6

So the supervisor's only job is distrust: read every diff against actual source, revert anything it can't confirm by reading the code (refactors die on sight), finish crashed jobs from checkpoints, run the real build/tests itself because worker sandboxes can't.

## 5/6

Result: free workers do the generation, one strong model does the verification, composite ships zero errors where either alone would ship some. Verification is cheaper than generation — that's the whole economic argument.

## 6/6

Everything is open: a managed harness (doctor / run / status / wait), compact worker briefs, crash checkpoints with 2-minute heartbeats, and the measured failure modes. Drops into Claude Code as a skill via install.sh.

https://github.com/fortun8te/ocodex
