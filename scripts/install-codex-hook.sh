#!/bin/bash
# Install the llm-memory Codex hook (spec D4): SessionEnd and SubagentStop
# entries in $CODEX_HOME/hooks.json running scripts/codex-hook-ingest.sh, then
# persist trust for them in $CODEX_HOME/config.toml the way Codex's own TUI
# does (hooks.state.<key>.trusted_hash = the hash hooks/list reports), so the
# hooks run for non-interactive `codex exec` sessions too. Idempotent. Run on
# each machine whose Codex sessions should land in the store; the repo, its
# .venv, and config/db-config.ini must exist there.
set -e
GIT_ROOT=$(git rev-parse --show-toplevel)
PY="$GIT_ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "FATAL: $PY not found. Run: uv sync" >&2; exit 1; }
HOOK="$GIT_ROOT/scripts/codex-hook-ingest.sh"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
export CODEX_HOME
"$PY" - "$CODEX_HOME" "$HOOK" <<'EOF2'
import json, re, select, subprocess, sys, time
from pathlib import Path

home, hook = Path(sys.argv[1]), sys.argv[2]
hooks_path = home / "hooks.json"
doc = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
events = doc.setdefault("hooks", {})
for event in ("SessionEnd", "SubagentStop"):
    groups = events.setdefault(event, [])
    if not groups:
        groups.append({"hooks": []})
    handlers = groups[0].setdefault("hooks", [])
    # Replace any earlier variant of this hook rather than accumulate them.
    handlers[:] = [h for h in handlers if "llm-memory" not in h.get("command", "") and "codex-hook-ingest" not in h.get("command", "")]
    handlers.append({"type": "command", "command": hook})
hooks_path.write_text(json.dumps(doc, indent=2) + "\n")
print(f"hooks.json: SessionEnd and SubagentStop -> {hook}")

# Ask Codex for the hooks' keys and hashes, then persist trust for ours.
p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
def send(o):
    p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()
def wait(i, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([p.stdout], [], [], 0.5)
        if r:
            line = p.stdout.readline()
            if not line:
                break
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == i:
                return m
    raise SystemExit("FATAL: codex app-server did not answer hooks/list")
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "llm-memory", "title": "llm-memory", "version": "0"}}}); wait(1)
send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
send({"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {}}); listed = wait(2)
p.terminate()
ours = [h for group in listed["result"]["data"] for h in group["hooks"] if h["command"] == hook]
if len(ours) < 2:
    raise SystemExit(f"FATAL: expected our two hooks in hooks/list, saw {len(ours)}")

cfg = home / "config.toml"
text = cfg.read_text() if cfg.exists() else ""
for h in ours:
    key, digest = h["key"], h["currentHash"]
    text = re.sub(r'\n?\[hooks\.state\."' + re.escape(key) + r'"\]\n(?:[a-z_]+ = [^\n]*\n)*', "", text)
    text = text.rstrip("\n") + f'\n\n[hooks.state."{key}"]\nenabled = true\ntrusted_hash = "{digest}"\n'
cfg.write_text(text)
for h in ours:
    print(f"trusted: {h['eventName']} {h['currentHash']}")
EOF2
