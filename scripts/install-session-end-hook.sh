#!/bin/bash
# Install the khipumaq/llm-memory SessionEnd ingest hook into
# ~/.claude/settings.json (spec D4, layer 1). Idempotent. Run on each machine
# whose sessions should land in the store; the repo, its .venv, and
# config/db-config.ini must exist there.
set -e
GIT_ROOT=$(git rev-parse --show-toplevel)
PY="$GIT_ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "FATAL: $PY not found. Run: uv sync" >&2; exit 1; }
# The package is not installed into the venv; it imports from the repo root.
# The hook runs with the ending session's cwd, so put the repo on the path.
# Import main explicitly rather than `python -m`: on a checkout that predates
# the CLI, `-m` runs the module, prints nothing, and exits 0 — a silent no-op
# hook. The import form fails loudly instead.
CMD="PYTHONPATH=$GIT_ROOT timeout 30 $PY -c 'import sys; from llm_memory.ingest import main; sys.exit(main([\"claude-session\"]))'"
SETTINGS="$HOME/.claude/settings.json"
python3 - "$SETTINGS" "$CMD" <<'EOF'
import json, sys
path, cmd = sys.argv[1], sys.argv[2]
s = json.load(open(path))
groups = s.setdefault("hooks", {}).setdefault("SessionEnd", [])
if not groups:
    groups.append({"hooks": []})
hooks = groups[0].setdefault("hooks", [])
if any(h.get("command") == cmd for h in hooks):
    print("already installed:", cmd)
else:
    # Replace any earlier variant of this hook rather than accumulate them.
    hooks[:] = [h for h in hooks if "llm_memory.ingest" not in h.get("command", "")]
    hooks.append({"type": "command", "command": cmd})
    json.dump(s, open(path, "w"), indent=2)
    print("installed:", cmd)
EOF
