#!/usr/bin/env python3
"""contextpack.py — build a compact CONTEXT.md for a repo.

Emits a deterministic, ~2000-word-capped context pack containing:
  - a git facts header (commit, branch, dirty state)
  - a tree summary (dirs + top-level files, depth-limited)
  - heads of key files (README, entrypoints, configs — largest/most central first)
  - conventions detected from the real files (language mix, style markers)
  - word count and generation provenance

Usage:
  python3 contextpack.py /path/to/repo --out CONTEXT.md
  python3 contextpack.py /path/to/repo --check          # staleness check
  python3 contextpack.py /path/to/repo --check --strict # nonzero exit if stale

--check compares the commit recorded in CONTEXT.md against HEAD. If HEAD
matches but the working tree is dirty relative to that commit, it reports
"dirty" (content may have changed without a new commit).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

WORD_CAP = 2000
TREE_DEPTH = 2
HEAD_LINES = 30
HEAD_FILES = 8

# Files considered "key" by name or extension priority.
KEY_NAMES = {
    "README.md", "README", "AGENTS.md", "CLAUDE.md", "SUPERVISOR.md",
    "SKILL.md", "Makefile", "package.json", "pyproject.toml",
    "setup.py", "setup.cfg", "Cargo.toml", "go.mod", "requirements.txt",
}
KEY_EXTS = [".py", ".ts", ".tsx", ".js", ".rs", ".go", ".sh"]

CONVENTION_PROBES = [
    ("type hints", r"from __future__ import annotations|: \w+ -> "),
    ("argparse CLI", r"import argparse"),
    ("pathlib paths", r"from pathlib import Path"),
    ("pytest tests", r"def test_"),
    ("docstring headers", r'^"""'),
]


def run_git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def git_facts(repo: Path) -> dict[str, str]:
    return {
        "commit": run_git(repo, "rev-parse", "HEAD") or "unknown",
        "branch": run_git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "dirty": run_git(repo, "status", "--porcelain"),
        "last_subject": run_git(repo, "log", "-1", "--pretty=%s") or "",
    }


def tree_summary(repo: Path) -> list[str]:
    lines: list[str] = []
    dirs = sorted(p for p in repo.rglob("*")
                  if p.is_dir()
                  and ".git" not in p.parts
                  and "__pycache__" not in p.parts
                  and len(p.relative_to(repo).parts) <= TREE_DEPTH)
    for d in dirs:
        rel = d.relative_to(repo)
        entries = sorted(x.name for x in d.iterdir()
                         if x.is_file() and not x.name.startswith("."))
        count = len(list(d.iterdir()))
        suffix = f" ({len(entries)} files: {', '.join(entries[:6])}{'…' if len(entries) > 6 else ''})" if entries else ""
        lines.append(f"- {rel}/{' …' if count != len(entries) else ''}{suffix}")
    top_files = sorted(p.name for p in repo.iterdir() if p.is_file())
    if top_files:
        lines.insert(0, f"(root files: {', '.join(top_files)})")
    return lines


def key_files(repo: Path) -> list[Path]:
    candidates = [
        p for p in repo.rglob("*")
        if p.is_file()
        and ".git" not in p.parts
        and "__pycache__" not in p.parts
        and (p.name in KEY_NAMES or p.suffix in KEY_EXTS)
        and p.stat().st_size < 200_000  # skip generated/huge blobs
    ]
    def rank(p: Path) -> tuple[int, int]:
        depth = len(p.relative_to(repo).parts)
        name_bonus = 0 if p.name in KEY_NAMES else 1
        return (name_bonus, depth)
    candidates.sort(key=rank)
    return candidates[:HEAD_FILES]


def file_head(path: Path) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return "(unreadable)"
    body = "\n".join(lines[:HEAD_LINES])
    more = f"\n… (+{max(0, len(lines) - HEAD_LINES)} more lines)" if len(lines) > HEAD_LINES else ""
    return f"```\n{body}\n```{more}"


def conventions(files: list[Path]) -> list[str]:
    found: list[str] = []
    blob = "\n".join(
        p.read_text(errors="replace") for p in files
        if p.stat().st_size < 100_000
    )
    for label, pattern in CONVENTION_PROBES:
        if re.search(pattern, blob, re.MULTILINE):
            found.append(f"- {label}: detected")
    langs: dict[str, int] = {}
    for p in files:
        langs[p.suffix] = langs.get(p.suffix, 0) + 1
    if langs:
        mix = ", ".join(f"{k or 'no-ext'} x{v}" for k, v in sorted(langs.items(), key=lambda x: -x[1]))
        found.append(f"- language mix across key files: {mix}")
    return found


def build_pack(repo: Path) -> str:
    g = git_facts(repo)
    tree = tree_summary(repo)
    kf = key_files(repo)
    parts = [
        "# CONTEXT pack\n",
        f"_Generated deterministically by contextpack.py. Word cap {WORD_CAP}. "
        "Facts below reflect the recorded commit; verify anything critical against source._\n",
        "## Git facts",
        f"- commit: `{g['commit']}`",
        f"- branch: `{g['branch']}`",
        f"- dirty: {'YES — uncommitted changes present' if g['dirty'] else 'no'}",
        f"- last commit subject: {g['last_subject']}\n",
        "## Tree summary",
        *(tree or ["(empty)"]),
        "\n## Key file heads",
    ]
    for p in kf:
        rel = p.relative_to(repo)
        parts.append(f"\n### {rel}")
        parts.append(file_head(p))
    parts.append("\n## Conventions detected from real files")
    conv = conventions(kf) or ["- none detected"]
    parts.extend(conv)
    text = "\n".join(parts)
    words = text.split()
    if len(words) > WORD_CAP:
        text = "\n".join(text.split()[:WORD_CAP]) + "\n\n_(truncated at word cap)_"
    wc = min(len(words), WORD_CAP)
    text += f"\n\n---\nword_count: {wc} | cap: {WORD_CAP} | commit_at_build: {g['commit']}\n"
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", type=Path)
    ap.add_argument("--out", default="CONTEXT.md", help="output path (default ./CONTEXT.md)")
    ap.add_argument("--check", action="store_true", help="verify existing pack freshness only")
    ap.add_argument("--strict", action="store_true", help="with --check: exit 1 when stale/dirty")
    args = ap.parse_args()

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"not a directory: {repo}", file=sys.stderr)
        return 2

    if args.check:
        out_path = Path(args.out)
        if not out_path.exists():
            print(f"STALE/MISSING: no pack at {out_path}", file=sys.stderr)
            return 1
        existing = out_path.read_text(errors="replace")
        marker = next((ln for ln in existing.splitlines() if ln.startswith("commit_at_build:") or "commit_at_build:" in ln), None)
        if not marker:
            print("STALE: pack has no commit marker (older format)", file=sys.stderr)
            return 1
        built_commit = marker.split("commit_at_build:", 1)[1].strip().split()[0]
        head_now = run_git(repo, "rev-parse", "HEAD")
        dirty = run_git(repo, "status", "--porcelain")
        if built_commit != head_now:
            print(f"STALE: pack built at {built_commit}, HEAD is {head_now}")
            return 1 if args.strict else 0
        if dirty:
            print(f"DIRTY: commit matches ({head_now}) but working tree has uncommitted changes")
            return 1 if args.strict else 0
        print(f"FRESH: pack matches HEAD {head_now}")
        return 0

    pack = build_pack(repo)
    out_path = Path(args.out)
    out_path.write_text(pack)
    wc = len(pack.split())
    print(f"wrote {out_path} (~{wc} words)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
