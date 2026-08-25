#!/usr/bin/env python3
"""CLAIM | C|I | claim | path:line | \"snippet\" — parse and mechanically check."""
from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

CLAIM_RE = re.compile(
    r"^CLAIM\s*\|\s*([CIci])\s*\|\s*(.*?)\s*\|\s*(\S+)\s*\|\s*\"([^\"]*)\"\s*$"
)


def parse_claims(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        match = CLAIM_RE.match(line.strip())
        if not match:
            continue
        pointer = match.group(3)
        path, _, line_no = pointer.partition(":")
        try:
            lineno = int(line_no) if line_no else None
        except ValueError:
            lineno = None
        rows.append({
            "tag": match.group(1).upper(),
            "claim": match.group(2).strip(),
            "path": path,
            "line": lineno,
            "pointer": pointer,
            "snippet": match.group(4),
        })
    return rows


def _fuzzy(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def check_claim(row: dict[str, Any], workdir: Path) -> dict[str, Any]:
    verdict = {
        **row,
        "verdict": "ok",
        "detail": "",
    }
    if row.get("tag") == "I":
        verdict["verdict"] = "inferred"
        verdict["detail"] = "I-tagged; supervisor discards unless independently confirmed"
        return verdict
    path = (workdir / row["path"]).resolve() if row.get("path") else None
    if path is None or not path.is_file():
        verdict["verdict"] = "missing"
        verdict["detail"] = f"file not found: {row.get('path')}"
        return verdict
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        verdict["verdict"] = "missing"
        verdict["detail"] = str(exc)
        return verdict
    snippet = (row.get("snippet") or "").strip()
    if not snippet:
        verdict["verdict"] = "mismatch"
        verdict["detail"] = "empty snippet"
        return verdict
    lineno = row.get("line")
    if isinstance(lineno, int) and lineno >= 1:
        lo = max(0, lineno - 4)
        hi = min(len(lines), lineno + 3)
        window = lines[lo:hi]
    else:
        window = lines[:200]
    best = max((_fuzzy(snippet, line) for line in window), default=0.0)
    if best >= 0.55 or any(snippet.lower() in line.lower() for line in window):
        verdict["verdict"] = "ok"
        verdict["detail"] = f"match {best:.2f}"
        return verdict
    verdict["verdict"] = "mismatch"
    verdict["detail"] = f"snippet not near {row.get('pointer')} (best {best:.2f})"
    return verdict


def check_text(text: str, workdir: Path) -> dict[str, Any]:
    rows = [check_claim(row, workdir) for row in parse_claims(text)]
    counts = {"ok": 0, "inferred": 0, "missing": 0, "mismatch": 0}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    failed = counts["missing"] + counts["mismatch"]
    return {
        "claims": rows,
        "counts": counts,
        "schema_compliant": bool(rows),
        "evidence_failed": failed > 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--workdir", type=Path)
    args = parser.parse_args()
    out = args.out_dir.expanduser()
    workdir = (args.workdir or Path(".")).expanduser()
    reports = []
    for final in sorted(out.glob("*.final.txt")):
        name = final.name[: -len(".final.txt")]
        report = check_text(final.read_text(encoding="utf-8", errors="replace"), workdir)
        report["name"] = name
        path = out / f"{name}.claims.report.json"
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        reports.append(report)
    print(json.dumps({"out": str(out), "agents": reports}, indent=2))
    return 1 if any(item.get("evidence_failed") for item in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
