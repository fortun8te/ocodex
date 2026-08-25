#!/usr/bin/env python3
"""Mechanical post-run gate. Emits triage.json so the supervisor skips corpses."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from claims import check_text, parse_claims
from harness_lib import _load_json


def _git_names(workdir: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(workdir), "diff", "--name-only"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _owns_set(owns: list[str], workdir: Path) -> list[Path]:
    base = workdir.resolve() if workdir.exists() else workdir
    out: list[Path] = []
    for item in owns:
        path = Path(item)
        resolved = path.resolve() if path.is_absolute() else (base / item)
        out.append(resolved)
    return out


def _inside(path: Path, owned: list[Path]) -> bool:
    for item in owned:
        try:
            if path == item or path.is_relative_to(item) or item.is_relative_to(path):
                return True
        except (ValueError, OSError):
            if str(path).startswith(str(item)):
                return True
    return False


def triage_agent(
    name: str,
    *,
    out: Path,
    workdir: Path,
    mode: str,
    owns: list[str],
    changed: list[str],
) -> dict[str, Any]:
    result = _load_json(out / f"{name}.result.json") or {}
    progress = out / f"{name}.progress.md"
    final = None
    for candidate in out.glob(f"**/{name}.final.txt"):
        final = candidate
        break
    if (out / f"{name}.final.txt").exists():
        final = out / f"{name}.final.txt"
    final_text = ""
    if final and final.exists():
        try:
            final_text = final.read_text(encoding="utf-8", errors="replace")
        except OSError:
            final_text = ""
    claims_report = check_text(final_text, workdir) if final_text else {
        "claims": [], "counts": {}, "schema_compliant": False, "evidence_failed": False,
    }
    parsed = claims_report.get("claims") or parse_claims(final_text)
    owned_paths = _owns_set(owns, workdir)
    owned_diff = []
    foreign_diff = []
    for rel in changed:
        full = (workdir / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
        if not owned_paths or _inside(full, owned_paths):
            owned_diff.append(rel)
        else:
            foreign_diff.append(rel)

    ok = result.get("ok")
    status = result.get("status")
    empty_final = not final_text.strip()
    if result.get("retrying"):
        verdict = "retrying"
    elif ok is False or status == "failed" or (empty_final and ok is not True):
        verdict = "crashed"
    elif claims_report.get("evidence_failed"):
        verdict = "evidence-failed"
    elif mode == "worker" and foreign_diff:
        verdict = "out-of-scope"
    elif mode == "worker" and not owned_diff and not empty_final:
        verdict = "no-diff"
    elif not parsed:
        verdict = "unverifiable"
    else:
        verdict = "ok-pending-supervisor"

    return {
        "name": name,
        "mode": mode,
        "verdict": verdict,
        "ok": ok,
        "status": status,
        "error_class": result.get("error_class"),
        "retries": result.get("retries", 0),
        "seconds": result.get("seconds"),
        "owned_diff_files": owned_diff,
        "foreign_diff_files": foreign_diff,
        "schema_compliant": bool(claims_report.get("schema_compliant")),
        "claims_counts": claims_report.get("counts") or {},
        "inferred": (claims_report.get("counts") or {}).get("inferred", 0),
        "checkpoint_exists": progress.exists(),
        "escalate": verdict in {
            "ok-pending-supervisor", "unverifiable", "out-of-scope", "evidence-failed", "no-diff",
        },
        "finish_from_checkpoint": verdict == "crashed",
    }


def collect_triage(out: Path, workdir: Path | None = None) -> dict[str, Any]:
    out = Path(out)
    ledger = _load_json(out / "ledger.json") or {}
    workdir = Path(workdir or ledger.get("workdir") or ".")
    modes = ledger.get("modes") or {}
    ownership = ledger.get("ownership") or {}
    names: list[str] = []
    for entry in ledger.get("chunk_plan") or []:
        for name in entry.get("agents") or []:
            if name not in names:
                names.append(name)
    for name in list(modes) + list(ownership):
        if name not in names:
            names.append(name)
    changed = _git_names(workdir) if workdir.exists() else []
    agents = []
    for name in names:
        agents.append(triage_agent(
            name,
            out=out,
            workdir=workdir,
            mode=str(modes.get(name) or "scout"),
            owns=list(ownership.get(name) or []),
            changed=changed,
        ))
    escalate = [item["name"] for item in agents if item.get("escalate")]
    crashed = [item["name"] for item in agents if item["verdict"] == "crashed"]
    return {
        "out": str(out),
        "workdir": str(workdir),
        "agents": {item["name"]: item for item in agents},
        "escalate": escalate,
        "crashed": crashed,
        "counts": {
            "total": len(agents),
            "escalate": len(escalate),
            "crashed": len(crashed),
        },
    }


def write_triage(out: Path, workdir: Path | None = None) -> dict[str, Any]:
    report = collect_triage(out, workdir)
    path = Path(out) / "triage.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["path"] = str(path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--workdir", type=Path)
    args = parser.parse_args()
    report = write_triage(args.out_dir, args.workdir)
    print(json.dumps({
        "path": report.get("path"),
        "counts": report.get("counts"),
        "escalate": report.get("escalate"),
        "crashed": report.get("crashed"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
