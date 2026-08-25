#!/usr/bin/env python3
"""Stub ocodex CLI for harness tests. No network, no keys."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def _outfile(argv: list[str]) -> str | None:
    if "-o" in argv:
        idx = argv.index("-o")
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return None


def _bump_count() -> int:
    path = Path(os.environ.get("FAKE_OCODEX_COUNT", "/tmp/fake-ocodex-count"))
    n = int(path.read_text(encoding="utf-8")) if path.exists() else 0
    n += 1
    path.write_text(str(n), encoding="utf-8")
    return n


def main() -> int:
    argv = sys.argv[1:]
    outfile = _outfile(argv)
    argv_dump = os.environ.get("FAKE_OCODEX_ARGV")
    if argv_dump:
        path = Path(argv_dump)
        prev = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(prev + " ".join(argv) + "\n", encoding="utf-8")
    # Persist a session id so the harness can `exec resume` on retry.
    sys.stdout.write(
        '{"type":"session_meta","payload":{"session_id":"11111111-1111-1111-1111-111111111111"}}\n'
    )
    sys.stdout.flush()
    stdin = sys.stdin.read()
    dump = os.environ.get("FAKE_OCODEX_STDIN")
    if dump:
        path = Path(dump)
        prev = path.read_text(encoding="utf-8") if path.exists() else ""
        sep = "\n=====NEXT ATTEMPT=====\n" if prev else ""
        path.write_text(prev + sep + stdin, encoding="utf-8")
    pidfile = os.environ.get("FAKE_OCODEX_PIDFILE")
    if pidfile:
        Path(pidfile).write_text(str(os.getpid()), encoding="utf-8")
    n = _bump_count()
    mode = os.environ.get("FAKE_OCODEX_MODE", "ok")

    if mode == "sleep":
        time.sleep(float(os.environ.get("FAKE_OCODEX_SLEEP", "60")))
        if outfile:
            Path(outfile).write_text("slept\n", encoding="utf-8")
        return 0
    if mode == "sleep_once":
        if n == 1:
            time.sleep(float(os.environ.get("FAKE_OCODEX_SLEEP", "60")))
            if outfile:
                Path(outfile).write_text("slept\n", encoding="utf-8")
            return 0
        if outfile:
            Path(outfile).write_text("resumed after stale heartbeat\n", encoding="utf-8")
        return 0
    if mode == "fail_once":
        if n == 1:
            sys.stderr.write("stream disconnect\n")
            return 1
        if outfile:
            Path(outfile).write_text("recovered after retry\n", encoding="utf-8")
        return 0
    if mode == "fail_twice" or mode == "fail_always":
        sys.stderr.write("crash\n")
        return 1
    if mode == "fail_fatal":
        sys.stderr.write("invalid api key\n")
        return 1
    if mode == "claims":
        if outfile:
            Path(outfile).write_text(
                "ok\n"
                'CLAIM | C | README states the three rules | README.md:1 | "# ocodex"\n',
                encoding="utf-8",
            )
        return 0
    if mode == "empty":
        return 0
    if outfile:
        Path(outfile).write_text("ok from fake ocodex\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
