def new_ledger(
    *,
    out: Path,
    workdir: Path | str | None,
    agents: list[dict[str, Any]],
    chunks: list[list[dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "started_at": utc_now(),
        "out": str(out),
        "workdir": str(workdir) if workdir else None,
        "ownership": {agent["name"]: list(agent.get("owns") or []) for agent in agents},
        "modes": {agent["name"]: agent.get("mode", "scout") for agent in agents},
        "models": {agent["name"]: agent.get("model") for agent in agents},
        "tasks": {
            agent["name"]: (strip_checkpoint(agent.get("task") or "").splitlines() or [""])[0][:100]
            for agent in agents
        },
        "chunk_plan": [
            {
                "chunk": index,
                "agents": [agent["name"] for agent in chunk],
                "status": "pending",
                "exit_code": None,
                "finished_at": None,
            }
            for index, chunk in enumerate(chunks)
        ],
        "killed_by": None,
    }


def seed_progress_files(out: Path, agents: list[dict[str, Any]]) -> None:
    """Create \u003cout\u003e/\u003cname\u003e.progress.md immediately so a crash still leaves a file."""
    stamp = utc_now()
    for agent in agents:
        path = out / f"{agent['name']}.progress.md"
        if path.exists():
            continue
        path.write_text(
            f"# {agent['name']} checkpoint\n\n"
            f"HEARTBEAT {stamp} | launched | harness created checkpoint; write analysis before edits\n"
            f"- harness created this file at launch ({stamp}). "
            "Write analysis here BEFORE editing any owned file. "
            "Then one bullet per named sub-task or per file touched. "
            "Heartbeat at least every 2 minutes.\n",
            encoding="utf-8",
        )


def update_ledger_chunk(
    ledger: dict[str, Any],
    chunk_index: int,
    *,
    status: str,
    exit_code: int | None,
) -> dict[str, Any]:
    for entry in ledger.get("chunk_plan", []):
        if entry.get("chunk") == chunk_index:
            entry["status"] = status
            entry["exit_code"] = exit_code
            entry["finished_at"] = utc_now()
            break
    return ledger


def merge_result_json(
    path: Path,
    *,
    agent: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Write/merge \u003cname\u003e.result.json. Preserve worker-supplied claims if present."""
    payload: dict[str, Any] = {
        "name": agent["name"],
        "status": "ok" if result.get("ok") else "failed",
        "files_touched": [],
        "claims": [],
        "failed_scenarios": [],
        "ok": bool(result.get("ok")),
        "return_code": result.get("return_code"),
        "seconds": result.get("seconds"),
        "error_class": result.get("error_class"),
        "retries": result.get("retries", 0),
        "timed_out": bool(result.get("timed_out")),
        "retrying": bool(result.get("retrying")),
    }
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            for key in ("status", "files_touched", "claims", "failed_scenarios"):
                if key in existing and existing[key] not in (None, [], ""):
                    payload[key] = existing[key]
            if result.get("ok") is False:
                payload["status"] = "failed"
            elif result.get("ok") is True and payload.get("status") not in {"ok", "partial"}:
                # Do not keep a harness retrying-marker status=failed after a successful retry.
                payload["status"] = "ok"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


DEFAULT_WORKERS_PER_KEY = 6
RUNNER_BATCH_CAP = 6


def slug_name(text: str, fallback: str = "worker") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    cleaned = cleaned[:40] or fallback
    if not NAME_RE.fullmatch(cleaned):
        cleaned = fallback
    return cleaned


def generate_manifest(
    goal: str,
    workdir: Path | str,
    *,
    owns: list[str] | None = None,
    name: str | None = None,
    mode: str | None = None,
    facts: list[str] | str | None = None,
    effort: str | None = None,
) -> dict[str, Any]:
    """Build a one-agent manifest from a short goal. Worker if owns else scout."""
    if not goal or not str(goal).strip():
        fail("goal must be a non-empty string")
    owns = [item for item in (owns or []) if item]
    resolved_mode = mode or ("worker" if owns else "scout")
    if resolved_mode not in {"scout", "worker"}:
        fail("mode must be scout or worker")
    if resolved_mode == "worker" and not owns:
        fail("worker mode needs at least one --owns path")
    agent: dict[str, Any] = {
        "name": name or slug_name(goal, "agent"),
        "mode": resolved_mode,
        "task": str(goal).strip(),
    }
    if owns:
        agent["owns"] = owns
    if facts:
        agent["facts"] = facts
    if effort:
        agent["effort"] = effort
    workdir_path = Path(workdir).expanduser()
    if not workdir_path.is_absolute():
        workdir_path = (Path.cwd() / workdir_path).resolve()
    else:
        workdir_path = workdir_path.resolve() if workdir_path.exists() else workdir_path
    return {"workdir": str(workdir_path), "agents": [agent]}


def plan_capacity(
    n_agents: int,
    keys: int,
    remaining: int,
    workers_per_key: int = DEFAULT_WORKERS_PER_KEY,
    max_workers: int = 0,
    runner_batch_cap: int = RUNNER_BATCH_CAP,
) -> dict[str, Any]:
    """Pool-aware concurrency. Ceiling is keys * ~6, not 'spawn more workers'."""
    keys = max(1, int(keys))
    workers_per_key = max(1, int(workers_per_key))
    recommended = keys * workers_per_key
    cap = recommended
    over_recommend = False
    if max_workers:
        cap = max(1, int(max_workers))
        over_recommend = cap > recommended
    # 6/key is rate-limit headroom, not a crash ceiling. Honour explicit caps.
    chunk_size = max(1, min(runner_batch_cap, cap))
    waves = (n_agents + chunk_size - 1) // chunk_size if n_agents else 0
    unused = max(0, cap - min(n_agents, cap))
    return {
        "keys": keys,
        "remaining": remaining,
        "workers_per_key": workers_per_key,
        "pool_cap": recommended,
        "recommended": recommended,
        "cap": cap,
        "over_recommend": over_recommend,
        "clamped_to_pool": False,
        "agents": n_agents,
        "chunk_size": chunk_size,
        "waves": waves,
        "unused_slots": unused,
    }



def format_headroom(plan: dict[str, Any], est_need: int) -> str:
    lines = [
        "headroom:",
        f"  keys:              {plan['keys']}",
        f"  workers/key:       {plan['workers_per_key']}  (OpenRouter slot-pool rule of thumb)",
        f"  pool recommended:  {plan['pool_cap']}  (keys * workers/key; default 6/key is rate-limit headroom, not a crash)",
        f"  concurrency cap:   {plan['cap']}"
        + ("  [over recommended — 429s backoff/hop; allowed, not a crash]" if plan.get("over_recommend") else ""),
        f"  agents in batch:   {plan['agents']}",
        f"  waves:             {plan['waves']}  (packing, not extra parallelism)",
        f"  unused slots now:  {plan['unused_slots']}",
        f"  reqs est:          ~{est_need}  (labeled guess, not measured)",
        f"  requests left:     ~{plan['remaining']}  (today; OpenRouter resets daily)",
        "ceiling: default ~6 concurrent workers per OpenRouter key (override --workers-per-key 8, not 20).",
        "quality gate: one supervisor. scaling: more keys via orslot, or pack waves. 429 is a hop, not a crash.",
    ]
    if plan["agents"] > plan["cap"]:
        lines.append(
            f"NOTE: {plan['agents']} agents and cap {plan['cap']} -> {plan['waves']} waves. "
            "That is packing, not a bigger fleet on one key."
        )
    if plan["remaining"] < est_need:
        lines.append("WARNING: pool may run dry mid-batch — fewer agents or `orslot add`.")
    return "\n".join(lines)


def ownership_lines(agents: list[dict[str, Any]]) -> list[str]:
    lines = []
    for agent in agents:
        owns = agent.get("owns") or []
        if agent.get("mode") == "scout" and not owns:
            desc = "read-only (scout)"
        else:
            desc = ", ".join(owns) if owns else "(none)"
        lines.append(f"  - {agent['name']} ({agent.get('mode', 'scout')}): {desc}")
    return lines


def supervisor_brief(
    *,
    out: Path | str,
    workdir: Path | str,
    agents: list[dict[str, Any]],
    skill_dir: Path | str | None = None,
    verify: str | None = None,
    facts: str | None = None,
    off_limits: str | None = None,
) -> str:
    """Filled SUPERVISOR.md spawn prompt — the management-layer tax cut."""
    skill = Path(skill_dir) if skill_dir else Path(__file__).resolve().parent.parent
    doctrine = skill / "SUPERVISOR.md"
    if not doctrine.exists():
        doctrine = Path.home() / ".claude/skills/ocodex/SUPERVISOR.md"
    wait_py = Path(__file__).resolve().parent / "wait_done.py"
    verify = verify or (
        "Run the real project build/tests yourself. Worker 'build passed' claims are not evidence."
    )
    facts = facts or "(none supplied — verify every claim against source)"
    off_limits = off_limits or (
        "anything not in this batch's ownership (see ledger.json); "
        "concurrent batches and the main loop are expected in git status"
    )
    owns = "\n".join(ownership_lines(agents)) or "  (no agents)"
    return "\n".join([
        "===== SUPERVISOR BRIEF (spawn one strong model with this) =====",
        f"Read {doctrine} and follow it exactly.",
        f"OUT={out}",
        f"Repo: {workdir}",
        "Workers and ownership:",
        owns,
        f"Ownership is also on disk: {out}/ledger.json",
        f"Concurrent work (git status, off-limits): {off_limits}",
        f"Real verification commands + expected results: {verify}",
        f"Facts that are authoritative: {facts}",
        "",
        "Wait (once, not a sleep loop):",
        f"  python3 {wait_py} {out} --timeout 1500",
        "Then parse \u003cname\u003e.result.json, git diff owned files, finish dead workers",
        "from \u003cname\u003e.progress.md, run the real build. Do not commit.",
        "===== END SUPERVISOR BRIEF =====",
    ])
