#!/bin/sh
# Codex SessionEnd / SubagentStop hook (spec D4): hand the hook JSON on stdin
# to a detached ingest of the rollout it names. Codex caps end-of-session
# hooks at about one second and kills them at exit; a large rollout takes
# longer to write, so the hook returns at once and the detached process
# finishes on its own. Its output goes to the log below; the sweep is the
# retry for anything that fails here.
ROOT=$(cd "$(dirname "$0")/.." && pwd)
LOG="${CODEX_HOME:-$HOME/.codex}/log/llm-memory-hook.log"
json=$(cat)
setsid sh -c '
  printf %s "$0" | PYTHONPATH="$1" timeout 300 "$1/.venv/bin/python" \
    -c "import sys; from llm_memory.ingest import main; sys.exit(main([\"codex\"]))" >> "$2" 2>&1
' "$json" "$ROOT" "$LOG" </dev/null >/dev/null 2>&1 &
