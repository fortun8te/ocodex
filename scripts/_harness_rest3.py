def _load_stats_by_name(path: Path) -> dict[str, dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return by_name
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("name"):
                by_name[rec["name"]] = rec
    except OSError:
        return by_name
    return by_name


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def collect_status(out: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Build one status row per agent from ledger + progress + result + stats. No LLM."""
    now = now or datetime.now(timezone.utc)
    out = Path(out)
    ledger = _load_json(out / "ledger.json") or {}
    started = parse_iso(ledger.get("started_at"))
    elapsed = age_seconds(started, now)
    all_done = (out / "all.done").read_text(encoding="utf-8").strip() if (out / "all.done").exists() else ""
    stats = _load_stats_by_name(out / "stats.jsonl")
    modes = ledger.get("modes") or {}
    tasks = ledger.get("tasks") or {}
    models = ledger.get("models") or {}
    names: list[str] = []
    for entry in ledger.get("chunk_plan") or []:
        for name in entry.get("agents") or []:
            if name not in names:
                names.append(name)
    for name in list(modes) + list(tasks):
        if name not in names:
            names.append(name)
    # Also pick up progress files not in ledger (partial runs).
    for progress in sorted(out.glob("*.progress.md")):
        name = progress.name[: -len(".progress.md")]
        if name not in names:
            names.append(name)

    rows: list[dict[str, Any]] = []
    for name in names:
        progress = parse_progress(out / f"{name}.progress.md")
        result = _load_json(out / f"{name}.result.json") or {}
        rec = stats.get(name) or {}
        hb_ts = parse_iso(progress.get("heartbeat_ts"))
        hb_age = age_seconds(hb_ts, now)
        result_status = result.get("status")
        ok = result.get("ok")
        if ok is None:
            ok = rec.get("ok")
        retries = rec.get("retries") or result.get("retries") or 0
        finished_batch = bool(all_done)
        interval = heartbeat_interval_sec()
        if result_status == "ok" or ok is True:
            state = "done"
        elif result.get("retrying"):
            state = "retrying"
        elif result_status == "failed" or (finished_batch and ok is False):
            state = "dead"
        elif retries and not finished_batch and ok is False:
            state = "retrying"
        elif hb_age is not None and hb_age > interval:
            state = "STALE"
        elif hb_age is None and elapsed is not None and elapsed > interval:
            state = "STALE"
        elif finished_batch and not result and not rec:
            state = "dead"
        else:
            state = "alive"
        slot = rec.get("slot") or rec.get("key_slot") or "-"
        model = rec.get("model") or models.get(name) or "-"
        api = f"{model}" if slot == "-" else f"{model} / {slot}"
        focus = progress.get("focus") or tasks.get(name) or ""
        rows.append({
            "name": name,
            "mode": modes.get(name) or rec.get("mode") or "-",
            "focus": focus,
            "subtask": progress.get("subtask") or "-",
            "last_update": progress.get("last_update") or "",
            "elapsed": format_age(elapsed),
            "elapsed_sec": elapsed,
            "heartbeat": progress.get("heartbeat_ts") or "-",
            "heartbeat_age": format_age(hb_age),
            "heartbeat_age_sec": hb_age,
            "state": state,
            "api": api,
            "model": model,
            "slot": slot,
            "ok": ok,
            "retries": retries,
        })
    return rows


def format_status_table(rows: list[dict[str, Any]], *, out: Path | str | None = None) -> str:
    headers = ["NAME", "MODE", "STATE", "FOCUS", "SUBTASK", "LAST UPDATE", "ELAPSED", "HEARTBEAT", "AGE", "API/SLOT"]
    if not rows:
        body = "(no agents found — check ledger.json / *.progress.md)"
        title = f"ocodex slot board  out={out}" if out else "ocodex slot board"
        return title + "\n" + body

    def trunc(value: object, width: int) -> str:
        text = "" if value is None else str(value)
        text = " ".join(text.split())
        return text if len(text) <= width else text[: max(0, width - 1)] + "…"

    widths = [12, 6, 8, 22, 14, 24, 8, 20, 8, 18]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        hb = row.get("heartbeat") or "-"
        vals = [
            trunc(row.get("name"), widths[0]),
            trunc(row.get("mode"), widths[1]),
            trunc(row.get("state"), widths[2]),
            trunc(row.get("focus"), widths[3]),
            trunc(row.get("subtask"), widths[4]),
            trunc(row.get("last_update"), widths[5]),
            trunc(row.get("elapsed"), widths[6]),
            trunc(hb, widths[7]),
            trunc(row.get("heartbeat_age"), widths[8]),
            trunc(row.get("api"), widths[9]),
        ]
        lines.append("  ".join(v.ljust(w) for v, w in zip(vals, widths)))
    title = f"ocodex slot board  out={out}  heartbeat stale after {int(heartbeat_interval_sec())}s"
    return title + "\n" + "\n".join(lines)
