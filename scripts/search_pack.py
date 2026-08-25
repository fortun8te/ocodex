#!/usr/bin/env python3
"""Harvest first, then pack read-only scouts. Never grants write.

Local: ripgrep (or a python walk) dumps hits; each scout only judges its pile.
Web: SearXNG dumps URLs; each scout only judges its URLs.
The model does not wander. That is the multiplier.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from harness_lib import ManifestError, searxng_url, slug_name

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".build", "target", ".ocodex", "out",
}
SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".woff", ".woff2", ".class", ".pyc", ".so", ".dylib", ".mp4", ".mov",
}
DEFAULT_MAX_FILES = 80
DEFAULT_MAX_HITS = 400
DEFAULT_HITS_PER_SCOUT = 40
DEFAULT_MAX_SCOUTS = 6


def pack_count(n_items: int, max_scouts: int, hits_per: int) -> int:
    if n_items <= 0:
        return 0
    by_size = (n_items + hits_per - 1) // hits_per
    return max(1, min(max_scouts, by_size, n_items))


def _rel(path: Path, workdir: Path) -> str:
    try:
        return str(path.resolve().relative_to(workdir.resolve()))
    except ValueError:
        return str(path)


def harvest_local(
    workdir: Path,
    query: str,
    *,
    glob: str | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_hits: int = DEFAULT_MAX_HITS,
) -> list[dict[str, Any]]:
    if not query or not query.strip():
        raise ManifestError("search query must be non-empty")
    workdir = workdir.expanduser().resolve()
    if not workdir.is_dir():
        raise ManifestError(f"workdir is not a directory: {workdir}")
    rg = shutil.which("rg")
    if rg:
        return _harvest_rg(workdir, query, glob=glob, max_files=max_files, max_hits=max_hits)
    return _harvest_walk(workdir, query, glob=glob, max_files=max_files, max_hits=max_hits)


def _harvest_rg(
    workdir: Path,
    query: str,
    *,
    glob: str | None,
    max_files: int,
    max_hits: int,
) -> list[dict[str, Any]]:
    cmd = [
        "rg", "-n", "--hidden", "--glob", "!.git/**",
        "--max-count", "20",
        query,
    ]
    if glob:
        cmd.extend(["--glob", glob])
    try:
        proc = subprocess.run(
            cmd, cwd=str(workdir), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManifestError(f"ripgrep failed: {exc}") from exc
    hits: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for line in proc.stdout.splitlines():
        path_s, sep, rest = line.partition(":")
        if not sep:
            continue
        line_s, sep2, text = rest.partition(":")
        if not sep2:
            continue
        rel = path_s.strip()
        if rel not in seen_files and len(seen_files) >= max_files:
            continue
        seen_files.add(rel)
        try:
            lineno = int(line_s)
        except ValueError:
            lineno = 0
        hits.append({"kind": "file", "path": rel, "line": lineno, "text": text.strip()[:240]})
        if len(hits) >= max_hits:
            break
    return hits


def _harvest_walk(
    workdir: Path,
    query: str,
    *,
    glob: str | None,
    max_files: int,
    max_hits: int,
) -> list[dict[str, Any]]:
    needle = query.lower()
    glob_re = None
    if glob:
        # glob is like "*.md" — turn into a suffix/name test
        glob_re = glob
    hits: list[dict[str, Any]] = []
    files_with_hits: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(workdir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() in SKIP_EXTS:
                continue
            if glob_re and glob_re.startswith("*.") and not name.endswith(glob_re[1:]):
                continue
            rel = _rel(path, workdir)
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            matched = False
            for index, line in enumerate(text.splitlines(), start=1):
                if needle not in line.lower() and not _re_search(query, line):
                    continue
                hits.append({
                    "kind": "file",
                    "path": rel,
                    "line": index,
                    "text": line.strip()[:240],
                })
                matched = True
                if len(hits) >= max_hits:
                    return hits
            if matched:
                files_with_hits.add(rel)
                if len(files_with_hits) >= max_files:
                    return hits
    return hits


def _re_search(query: str, line: str) -> bool:
    try:
        return re.search(query, line, re.IGNORECASE) is not None
    except re.error:
        return False


def harvest_web(
    query: str,
    *,
    limit: int = 20,
    timeout: float = 8.0,
) -> list[dict[str, Any]]:
    if not query or not query.strip():
        raise ManifestError("search query must be non-empty")
    base = searxng_url()
    url = (
        base.rstrip("/")
        + "/search?"
        + urllib.parse.urlencode({"q": query, "format": "json"})
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise ManifestError(f"SearXNG harvest failed at {base}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError("SearXNG did not return JSON (is format=json enabled?)") from exc
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    hits: list[dict[str, Any]] = []
    for item in results[:limit]:
        if not isinstance(item, dict):
            continue
        href = str(item.get("url") or "").strip()
        if not href:
            continue
        hits.append({
            "kind": "url",
            "path": href,
            "line": 0,
            "text": (str(item.get("title") or "") + " — " + str(item.get("content") or "")).strip()[:300],
            "title": str(item.get("title") or ""),
        })
    return hits


def _chunks(items: list[Any], n: int) -> list[list[Any]]:
    if n <= 0:
        return []
    if n == 1:
        return [items]
    size = (len(items) + n - 1) // n
    return [items[i:i + size] for i in range(0, len(items), size)]


def scout_task(query: str, pile: list[dict[str, Any]], *, kind: str) -> str:
    lines = []
    for hit in pile:
        if kind == "web":
            lines.append(f"- {hit.get('path')} | {hit.get('text')}")
        else:
            lines.append(f"- {hit.get('path')}:{hit.get('line')} | {hit.get('text')}")
    body = "\n".join(lines) if lines else "(no hits)"
    where = "these URLs" if kind == "web" else "these file:line hits"
    return (
        f"READ-ONLY scout. Do not edit files, do not create files, do not mutate anything.\n"
        f"QUERY: {query}\n"
        f"You are judging PRE-HARVESTED hits. Do not wander. Do not search beyond {where}.\n"
        f"For each hit: CONFIRMED (the quote is real and actually answers the query), "
        f"PLAUSIBLE, or JUNK.\n"
        f"CONFIRMED only as CLAIM | C | ... lines. I-tags are discarded.\n"
        f"STOP when the pile is labelled. Do not spawn subagents.\n\n"
        f"HITS:\n{body}"
    )


def pack_manifest(
    query: str,
    hits: list[dict[str, Any]],
    *,
    workdir: Path | str,
    kind: str = "local",
    max_scouts: int = DEFAULT_MAX_SCOUTS,
    hits_per_scout: int = DEFAULT_HITS_PER_SCOUT,
    effort: str = "low",
) -> dict[str, Any]:
    if not hits:
        raise ManifestError(f"no hits for {query!r} — nothing to pack")
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for hit in hits:
        path = str(hit.get("path") or "")
        if path not in grouped:
            grouped[path] = []
            order.append(path)
        grouped[path].append(hit)
    n = max(1, min(int(max_scouts), len(order)))
    path_piles = _chunks(order, n)
    piles = []
    for paths in path_piles:
        pile: list[dict[str, Any]] = []
        for path in paths:
            pile.extend(grouped[path])
        piles.append(pile)
    base = slug_name(query, "search")
    agents = []
    for index, pile in enumerate(piles):
        paths: list[str] = []
        seen: set[str] = set()
        for hit in pile:
            path = str(hit.get("path") or "")
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
        name = f"{base}-{index + 1}"
        if not re.fullmatch(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$", name):
            name = f"search-{index + 1}"
        agent: dict[str, Any] = {
            "name": name,
            "mode": "scout",
            "effort": effort,
            "task": scout_task(query, pile, kind=kind),
            "facts": [
                "Hits below were harvested by a script. Do not invent extra files or URLs.",
                "This scout is read-only. Editing is a failure.",
            ],
        }
        if kind != "web" and paths:
            agent["owns"] = paths
        agents.append(agent)
    return {
        "workdir": str(Path(workdir).expanduser()),
        "kind": kind,
        "query": query,
        "agents": agents,
        "hit_count": len(hits),
        "scout_count": len(agents),
    }
