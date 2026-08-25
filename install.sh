#!/bin/bash
# Init + doctor: copy the skill, write a sample, check ocodex/orslot/docker/SearXNG.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"

copy_skill() {
    local DEST="$1"
    mkdir -p "$DEST/scripts" "$DEST/examples"
    cp "$SRC/SKILL.md" "$SRC/SUPERVISOR.md" "$DEST/"
    [[ -f "$SRC/README.md" ]] && cp "$SRC/README.md" "$DEST/"
    cp "$SRC/scripts/"*.py "$DEST/scripts/"
    [[ -f "$SRC/scripts/ocodex-status" ]] && cp "$SRC/scripts/ocodex-status" "$DEST/scripts/"
    chmod +x "$DEST/scripts/"*.py "$DEST/scripts/ocodex-status" 2>/dev/null || true
    if [[ -d "$SRC/examples" ]]; then
        cp "$SRC/examples/"* "$DEST/examples/" 2>/dev/null || true
    fi
    # Sample workdir must be absolute so a first launch works from any cwd.
    python3 - "$DEST" "$SRC" <<'PY'
import json, sys
from pathlib import Path
dest, src = Path(sys.argv[1]), Path(sys.argv[2])
sample = dest / "examples" / "sample-manifest.json"
if sample.exists():
    data = json.loads(sample.read_text())
    data["workdir"] = str(src)
    sample.write_text(json.dumps(data, indent=2) + "\n")
PY
    echo "installed -> $DEST"
}

for DEST in "$HOME/.claude/skills/ocodex" "$HOME/.codex/skills/ocodex"; do
    if [[ "$DEST" == *"/.codex/"* && ! -d "$HOME/.codex" ]]; then
        continue
    fi
    copy_skill "$DEST"
done

echo
echo "=== doctor ==="
set +e
python3 "$SRC/scripts/ocodex_managed.py" doctor
DOC=$?
set -e
echo
echo "First sample launch (needs ocodex on PATH):"
echo "  python3 $SRC/scripts/ocodex_managed.py run $SRC/examples/sample-manifest.json --out-dir /tmp/ocodex-sample"
echo "  python3 $SRC/scripts/ocodex_managed.py status /tmp/ocodex-sample"
echo "Slot board alias: $SRC/scripts/ocodex-status /tmp/ocodex-sample"
echo
if [[ "$DOC" -ne 0 ]]; then
    echo "doctor reported missing pieces (see MISS lines above)."
    echo "SearXNG: docker compose -f $SRC/examples/searxng-compose.yml up -d"
    echo "         export SEARXNG_URL=http://127.0.0.1:8080"
    echo "Then re-run: python3 $SRC/scripts/ocodex_managed.py doctor"
fi
exit "$DOC"
