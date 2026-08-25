#!/bin/bash
# Installs the ocodex-managed skill for Claude Code (and Codex, if present).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
for DEST in "$HOME/.claude/skills/ocodex-managed" "$HOME/.codex/skills/ocodex-managed"; do
    [[ "$DEST" == *"/.codex/"* && ! -d "$HOME/.codex" ]] && continue
    mkdir -p "$DEST/scripts"
    cp "$SRC/SKILL.md" "$SRC/SUPERVISOR.md" "$DEST/"
    cp "$SRC/scripts/"*.py "$DEST/scripts/"
    chmod +x "$DEST/scripts/"*.py
    echo "installed -> $DEST"
done
echo "Requires: an 'ocodex' CLI on PATH. Optional: orslot for multi-key pools."
