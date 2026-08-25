#!/bin/bash
# Installs the ocodex skill for Claude Code, Grok, Codex, and Cursor when present.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DESTS=("$HOME/.claude/skills/ocodex")
[[ -d "$HOME/.grok" ]] && DESTS+=("$HOME/.grok/skills/ocodex")
[[ -d "$HOME/.codex" ]] && DESTS+=("$HOME/.codex/skills/ocodex")
[[ -d "$HOME/.cursor" ]] && DESTS+=("$HOME/.cursor/skills/ocodex")
for DEST in "${DESTS[@]}"; do
    mkdir -p "$DEST/scripts"
    cp "$SRC/SKILL.md" "$SRC/SUPERVISOR.md" "$DEST/"
    cp "$SRC/scripts/"*.py "$DEST/scripts/"
    chmod +x "$DEST/scripts/"*.py
    echo "installed -> $DEST"
done
echo "Requires: an 'ocodex' CLI on PATH. Optional: orslot for multi-key pools."
echo "Watch a live batch: python3 $HOME/.claude/skills/ocodex/scripts/fleet_watch.py <OUT>"
