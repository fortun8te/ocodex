def append_stats(record: dict[str, Any], *paths: Path | str | None) -> None:
    """Append one JSON line to each path. Also to $OCODEX_STATS if set."""
    line = json.dumps(record, ensure_ascii=False)
    targets: list[Path] = []
    seen: set[str] = set()

    def add(raw: Path | str | None) -> None:
        if not raw:
            return
        path = Path(raw).expanduser()
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        targets.append(path)

    for raw in paths:
        add(raw)
    add(os.environ.get("OCODEX_STATS"))

    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read manifest: {exc}")
    if not isinstance(data, dict):
        fail("manifest must be a JSON object")
    return data


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def estimate_requests(agents: list[dict[str, Any]]) -> int:
    total = 0
    for agent in agents:
        mode = agent.get("mode", "scout")
        effort = agent.get("effort")
        table = REQUEST_GUESS.get(mode, REQUEST_GUESS["worker"])
        total += table.get(effort, table[None])
    return total


def validate_manifest(
    data: dict[str, Any],
    default_timeout: int = 900,
    *,
    max_agents: int | None = MAX_AGENTS,
    require_workdir: bool = True,
) -> tuple[Path, list[dict[str, Any]]]:
    """Validate required fields and disjoint owns across editing workers."""
    raw_workdir = data.get("workdir")
    if not isinstance(raw_workdir, str) or not raw_workdir:
        fail("manifest missing required field 'workdir' (non-empty path)")
    workdir = Path(raw_workdir).expanduser()
    if not workdir.is_absolute():
        workdir = Path.cwd() / workdir
    if require_workdir:
        if not workdir.is_dir():
            fail(f"workdir is not an existing directory: {workdir}")
        workdir = workdir.resolve()
    else:
        workdir = workdir.resolve() if workdir.exists() else workdir

    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        fail("manifest missing required field 'agents' (non-empty list)")
    if max_agents is not None and len(agents) > max_agents:
        fail(f"at most {max_agents} agents may run in one batch")

    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    owned: list[tuple[str, Path, str]] = []  # name, resolved, original

    for index, raw in enumerate(agents):
        if not isinstance(raw, dict):
            fail(f"agent {index} must be an object")
        name = raw.get("name")
        task = raw.get("task")
        mode = raw.get("mode", "scout")
        if name is None or name == "":
            fail(f"agent {index} missing required field 'name'")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            fail(f"agent {index} has an invalid name")
        if name in names:
            fail(f"duplicate agent name: {name}")
        names.add(name)
        if not isinstance(task, str) or not task.strip():
            fail(f"agent {name} missing required field 'task' (non-empty string)")
        if mode not in {"scout", "worker"}:
            fail(f"agent {name} mode must be scout or worker")

        owns = raw.get("owns", [])
        if not isinstance(owns, list) or not all(isinstance(item, str) and item for item in owns):
            fail(f"agent {name} owns must be a list of paths")
        if mode == "worker" and not owns:
            fail(f"worker {name} needs at least one owned path")

        base = workdir.resolve() if workdir.exists() else workdir
        for item in owns:
            owned_path = (base / item).resolve() if not Path(item).is_absolute() else Path(item).resolve()
            if workdir.exists() and not owned_path.is_relative_to(base):
                fail(f"worker ownership must stay inside workdir: {item}")
            if mode == "worker":
                for prev_name, previous, prev_item in owned:
                    if _paths_overlap(owned_path, previous):
                        fail(
                            f"ownership overlap: worker {name!r} and worker {prev_name!r} "
                            f"both own {item} (conflicts with {prev_item})"
                        )
                owned.append((name, owned_path, item))

        timeout = raw.get("timeout", default_timeout)
        if not isinstance(timeout, int) or timeout < 30:
            fail(f"agent {name} timeout must be an integer of at least 30 seconds")
        model = raw.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            fail(f"agent {name} model must be a non-empty string")
        effort = raw.get("effort")
        if effort is not None and effort not in {"low", "medium", "high", "xhigh"}:
            fail(f"agent {name} effort must be low, medium, high, or xhigh")

        facts = raw.get("facts")
        if facts is not None and not isinstance(facts, (str, list)):
            fail(f"agent {name} facts must be a string or list of strings")

        normalized.append({
            "name": name,
            "task": task.strip(),
            "mode": mode,
            "owns": owns,
            "timeout": timeout,
            "model": model,
            "effort": effort,
            "facts": facts,
            "stop": raw.get("stop"),
        })
    return workdir if workdir.exists() else workdir, normalized
