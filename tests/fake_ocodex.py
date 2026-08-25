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
    stdin = sys.stdin.read()
    dump = os.environ.get("FAKE_OCODEX_STDIN")
    if dump:
        Path(dump).write_text(stdin, encoding="utf-8")
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
    if mode == "empty":
        return 0
    if outfile:
        Path(outfile).write_text("ok from fake ocodex\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
