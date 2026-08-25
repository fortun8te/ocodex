#!/usr/bin/env python3
"""Live terminal view of an ocodex batch.

    python3 fleet_watch.py OUT           # refresh until all.done (or Ctrl-C)
    python3 fleet_watch.py OUT --once    # one snapshot
    python3 fleet_watch.py OUT --interval 1

Reads only the out-dir (status.json when the launcher is writing it, otherwise
ledger + checkpoints + live.json). Does not launch or kill anything.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fleet_state import collect_state, load_json, render_table  # noqa: E402


def snapshot_from(out: Path) -> dict:
    written = load_json(out / "status.json")
    if written.get("agents"):
        # Re-read checkpoints so "now" stays current even if the launcher
        # is mid-sleep between polls.
        live = collect_state(
            out,
            cap=written.get("cap") or 0,
            started_at=written.get("started_at"),
            events=written.get("events") or [],
        )
        # Prefer launcher-assigned states (queued vs running) when present.
        by_name = {a["name"]: a for a in written["agents"]}
        for agent in live["agents"]:
            src = by_name.get(agent["name"])
            if src and src.get("state"):
                agent["state"] = src["state"]
                if src.get("seconds") is not None:
                    agent["seconds"] = src["seconds"]
                if src.get("attempt"):
                    agent["attempt"] = src["attempt"]
                if src.get("goal") and not agent.get("goal"):
                    agent["goal"] = src["goal"]
        live["cap"] = written.get("cap") or live.get("cap") or 0
        live["started_at"] = written.get("started_at") or live.get("started_at")
        live["elapsed_s"] = written.get("elapsed_s") or live.get("elapsed_s")
        live["events"] = written.get("events") or live.get("events") or []
        return live
    return collect_state(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()
    out = args.out_dir.expanduser().resolve()
    if not out.is_dir():
        print(f"not a directory: {out}", file=sys.stderr)
        return 2

    def draw() -> str:
        return render_table(snapshot_from(out))

    if args.once or not sys.stdout.isatty():
        sys.stdout.write(draw())
        return 0

    try:
        while True:
            text = draw()
            sys.stdout.write("\033[2J\033[H" + text)
            sys.stdout.flush()
            if (out / "all.done").exists():
                # One last draw so the terminal shows the terminal state.
                time.sleep(0.2)
                sys.stdout.write("\033[2J\033[H" + draw())
                sys.stdout.flush()
                summary = (out / "all.done").read_text(encoding="utf-8").strip()
                print(f"\nall.done: {summary}")
                return 0 if summary == "done" else 1
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
