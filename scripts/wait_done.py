#!/usr/bin/env python3
"""Wait on <out>/all.done without an LLM sleep loop.

Exit codes:
  0  all.done is a clean "done"
  1  all.done records failure or kill
  2  usage error
  3  timeout
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def parse_started(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def summarize(out: Path, contents: str) -> None:
    print(contents.strip() or "(empty)")
    print(f"all.done={contents.strip()}")
    ledger_path = out / "ledger.json"
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ledger = None
        if isinstance(ledger, dict):
            started = ledger.get("started_at")
            print(f"started_at={started}")
            parsed = parse_started(started)
            if parsed is not None:
                elapsed = (datetime.now(timezone.utc) - parsed).total_seconds()
                print(f"elapsed_sec={int(elapsed)}")
            failed = [
                entry.get("chunk")
                for entry in ledger.get("chunk_plan") or []
                if entry.get("status") in {"failed", "killed"}
            ]
            if failed:
                print(f"failed_chunks={','.join(map(str, failed))}")
    stats = out / "stats.jsonl"
    if stats.exists():
        lines = [line for line in stats.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(f"stats_lines={len(lines)}")


def wait_for_done(out: Path, timeout: float, poll: float) -> int:
    marker = out / "all.done"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            contents = marker.read_text(encoding="utf-8")
            summarize(out, contents)
            text = contents.strip()
            if text == "done":
                return 0
            return 1
        time.sleep(poll)
    print(f"timeout after {int(timeout)}s waiting for {marker}", file=sys.stderr)
    return 3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out_dir", nargs="?", help="batch out-dir containing all.done")
    ap.add_argument("--out-dir", dest="out_dir_flag")
    ap.add_argument("--timeout", type=float, default=1500)
    ap.add_argument("--poll", type=float, default=2)
    args = ap.parse_args()
    raw = args.out_dir_flag or args.out_dir
    if not raw:
        print("usage: wait_done.py <out-dir> [--timeout SEC] [--poll SEC]", file=sys.stderr)
        return 2
    out = Path(raw).expanduser()
    if not out.is_dir():
        print(f"out-dir is not a directory: {out}", file=sys.stderr)
        return 2
    return wait_for_done(out, args.timeout, max(0.05, args.poll))


if __name__ == "__main__":
    raise SystemExit(main())
