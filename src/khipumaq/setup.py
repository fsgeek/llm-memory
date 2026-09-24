"""Idempotent install/uninstall of khipumaq's user-scope wiring on one machine.

Four places, because four programs read them:
- ~/.claude/settings.json: a SessionEnd hook that ingests the ending session.
- ~/.claude.json: the read-only MCP server, under mcpServers["khipumaq"].
- $CODEX_HOME/hooks.json + config.toml: SessionEnd/SubagentStop hooks that
  ingest the closing rollout, and the trust Codex requires before it runs them.
- ~/.config/systemd/user: a daily sweep timer, the retry for failed hooks.

Our entries are recognized by their command, not by a tag, so Codex's schema
never sees a key it does not know. Install also removes the pre-package
`llm_memory` entries, so on a machine that ran from a checkout, merging the
package and running `khipumaq install` once is the whole migration.

Writes are atomic and touch only our own entries: ~/.claude.json holds all of
Claude Code's state.
"""

import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

MCP_NAME = "khipumaq"
LEGACY_MCP_NAME = "llm-memory"
CODEX_EVENTS = ("SessionEnd", "SubagentStop")
_OURS = ("khipumaq ingest claude-session", "khipumaq codex-hook")
_LEGACY = ("llm_memory.ingest", "codex-hook-ingest")


def _checkout_root():
    """The source checkout this package runs from, or None when installed from
    a wheel. A checkout's venv binary is stable; uvx's cache is not ours."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").exists() else None


def command_prefix():
    """How hooks and the MCP entry invoke khipumaq on this machine. Absolute
    paths throughout: Claude Code and Codex spawn hooks with a minimal PATH.
    From a wheel, pinned to the installed version: the hooks run on every
    session end with every transcript in reach, so a later release runs only
    after the user runs `install` again, not whenever uvx resolves it."""
    if _checkout_root() is not None:
        return [str(Path(sys.executable).parent / "khipumaq")]
    return [shutil.which("uvx") or "uvx", "--from", f"khipumaq=={version('khipumaq')}", "khipumaq"]


def _claude_hook_command():
    return " ".join(command_prefix() + ["ingest", "claude-session"])


def _codex_hook_command():
    return " ".join(command_prefix() + ["codex-hook"])


def _mcp_entry():
    prefix = command_prefix()
    return {"type": "stdio", "command": prefix[0], "args": prefix[1:] + ["serve"], "env": {}}


def _is_ours(command):
    return any(s in command for s in _OURS + _LEGACY)


def _load(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); khipumaq left it untouched.") from exc


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_json(path, data):
    _atomic_write(path, json.dumps(data, indent=2) + "\n")


def _strip_hooks(doc, events):
    """Remove our (and legacy) handlers from `events`; drop groups and events
    left empty. Returns whether anything was removed."""
    removed = False
    hooks = doc.get("hooks", {})
    for event in events:
        groups = []
        for group in hooks.get(event, []):
            handlers = group.get("hooks", [])
            kept = [h for h in handlers if not _is_ours(h.get("command", ""))]
            removed |= len(kept) != len(handlers)
            if kept:
                groups.append({**group, "hooks": kept})
        if groups:
            hooks[event] = groups
        elif event in hooks:
            del hooks[event]
    if "hooks" in doc and not doc["hooks"]:
        del doc["hooks"]
    return removed


def _add_hook(doc, event, command, **fields):
    doc.setdefault("hooks", {}).setdefault(event, []).append(
        {"hooks": [{"type": "command", "command": command, **fields}]}
    )


# -- Claude Code ---------------------------------------------------------------

def install_claude(settings_path, mcp_config_path):
    settings = _load(settings_path)
    _strip_hooks(settings, ("SessionEnd",))
    # Fails loudly and does not retry (spec D4): the sweep is the retry.
    _add_hook(settings, "SessionEnd", _claude_hook_command(), timeout=30)
    _write_json(settings_path, settings)

    config = _load(mcp_config_path)
    _remove_legacy_mcp(config)
    config.setdefault("mcpServers", {})[MCP_NAME] = _mcp_entry()
    _write_json(mcp_config_path, config)


def _remove_legacy_mcp(config):
    """The pre-package server ran `python -m llm_memory.mcp_server` from a
    checkout, registered at user scope and in some projects."""
    scopes = [config] + list(config.get("projects", {}).values())
    for scope in scopes:
        servers = scope.get("mcpServers", {})
        legacy = servers.get(LEGACY_MCP_NAME)
        if legacy and "llm_memory.mcp_server" in " ".join(legacy.get("args", [])):
            del servers[LEGACY_MCP_NAME]


def uninstall_claude(settings_path, mcp_config_path):
    settings = _load(settings_path)
    if _strip_hooks(settings, ("SessionEnd",)):
        _write_json(settings_path, settings)
    config = _load(mcp_config_path)
    if config.get("mcpServers", {}).pop(MCP_NAME, None) is not None:
        _write_json(mcp_config_path, config)


# -- Codex ---------------------------------------------------------------------

def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def codex_binary():
    """Needed only to ask Codex for the trust hashes of our hooks; the hooks are
    run by whichever Codex (CLI or VS Code extension) ends the session. Looks
    past PATH because nvm and bun installs are often only on an interactive
    shell's PATH."""
    found = os.environ.get("CODEX_BIN") or shutil.which("codex")
    if found:
        return found
    home = Path.home()
    candidates = sorted(home.glob(".nvm/versions/node/*/bin/codex"), reverse=True)
    candidates += [home / "node_modules/.bin/codex", home / ".bun/bin/codex"]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _hooks_list(codex_bin, home):
    """Ask `codex app-server` for every hook's key and current hash. The binary's
    own directory goes first on PATH: an nvm install keeps `node` beside it,
    and `codex` is a node script. If app-server dies, its stderr is the error."""
    env = os.environ | {
        "CODEX_HOME": str(home),
        "PATH": os.pathsep.join([str(Path(codex_bin).parent), os.environ.get("PATH", "")]),
    }
    errors = tempfile.TemporaryFile("w+")
    proc = subprocess.Popen(
        [codex_bin, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=errors, text=True, env=env,
    )

    def failed(why):
        proc.kill()
        proc.wait()
        errors.seek(0)
        detail = errors.read().strip()[-800:]
        return RuntimeError(f"{codex_bin} app-server {why}" + (f":\n{detail}" if detail else ""))

    def send(message):
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except BrokenPipeError:
            raise failed("exited") from None

    def wait(request_id, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                raise failed("exited")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                return message
        raise failed("did not answer hooks/list")

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": MCP_NAME, "title": MCP_NAME, "version": "0"}}})
        wait(1)
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "hooks/list", "params": {}})
        listed = wait(2)
    finally:
        proc.terminate()
    return [h for group in listed["result"]["data"] for h in group["hooks"]]


def _set_trust(text, key, digest=None):
    """Remove the [hooks.state."key"] table; re-add it trusted when `digest`."""
    text = re.sub(r'\n?\[hooks\.state\."' + re.escape(key) + r'"\]\n(?:[a-z_]+ = [^\n]*\n)*', "", text)
    if digest:
        text = text.rstrip("\n") + f'\n\n[hooks.state."{key}"]\nenabled = true\ntrusted_hash = "{digest}"\n'
    return text


def install_codex(home, codex_bin):
    """Write our hooks, then persist trust for them the way Codex's own TUI does,
    so they also run for non-interactive `codex exec` sessions."""
    hooks_path = home / "hooks.json"
    doc = _load(hooks_path)
    _strip_hooks(doc, CODEX_EVENTS)
    command = _codex_hook_command()
    for event in CODEX_EVENTS:
        _add_hook(doc, event, command)
    _write_json(hooks_path, doc)

    ours = [h for h in _hooks_list(codex_bin, home) if h["command"] == command]
    if len(ours) != len(CODEX_EVENTS):
        raise RuntimeError(f"expected {len(CODEX_EVENTS)} khipumaq hooks in hooks/list, saw {len(ours)}")
    config = home / "config.toml"
    text = config.read_text() if config.exists() else ""
    for hook in ours:
        text = _set_trust(text, hook["key"], hook["currentHash"])
    _atomic_write(config, text)


def uninstall_codex(home):
    hooks_path = home / "hooks.json"
    doc = _load(hooks_path)
    if _strip_hooks(doc, CODEX_EVENTS):
        _write_json(hooks_path, doc)


# -- Codex hook entry ----------------------------------------------------------

def codex_hook(stdin=sys.stdin):
    """Codex caps end-of-session hooks at about one second and kills them at
    exit; a large rollout takes longer than that to write. Hand the hook JSON
    to a detached ingest in its own session and return at once. Its output goes
    to the log; the sweep is the retry for anything that fails there."""
    payload = stdin.read()
    log = codex_home() / "log" / "khipumaq-hook.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as out:
        child = subprocess.Popen(
            ["timeout", "300", sys.executable, "-m", "khipumaq.ingest", "codex"],
            stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True, text=True,
        )
        child.stdin.write(payload)
        child.stdin.close()
    return 0


# -- Nightly sweep (systemd user timer) ----------------------------------------

UNIT = "khipumaq-sweep"


def _unit_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def has_systemd_user():
    return shutil.which("systemctl") is not None and subprocess.run(
        ["systemctl", "--user", "is-system-running"], capture_output=True
    ).returncode in (0, 1)  # 1 = "degraded": running, with some failed unit


def install_timer():
    """Daily sweep; Persistent=true runs a missed one at the next boot, so a
    machine that was off tonight is caught when it wakes (spec D4 layer 2)."""
    command = " ".join(command_prefix() + ["sweep"])
    units = {
        f"{UNIT}.service": (
            "[Unit]\nDescription=khipumaq: ingest sessions the hooks missed\n\n"
            f"[Service]\nType=oneshot\nExecStart={command}\n"
        ),
        f"{UNIT}.timer": (
            "[Unit]\nDescription=khipumaq nightly sweep\n\n"
            "[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"
        ),
    }
    for name, text in units.items():
        _atomic_write(_unit_dir() / name, text)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"], check=True, capture_output=True)


def uninstall_timer():
    if not (_unit_dir() / f"{UNIT}.timer").exists():
        return
    subprocess.run(["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], capture_output=True)
    for suffix in ("service", "timer"):
        (_unit_dir() / f"{UNIT}.{suffix}").unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


# -- Windows clients on a WSL machine -----------------------------------------

def windows_client_configs(profile):
    """Claude Code on Windows and Claude Desktop read their MCP servers from
    these; each launches khipumaq in WSL through wsl.exe over stdio."""
    return [profile / ".claude.json", profile / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"]


def install_windows_clients(profile):
    entry = {"command": "wsl.exe", "args": ["-e"] + command_prefix() + ["serve"]}
    installed = []
    for path in windows_client_configs(profile):
        if path.exists():
            config = _load(path)
            config.setdefault("mcpServers", {})[MCP_NAME] = entry
            _write_json(path, config)
            installed.append(path)
    return installed


def uninstall_windows_clients(profile):
    for path in windows_client_configs(profile):
        config = _load(path)
        if config.get("mcpServers", {}).pop(MCP_NAME, None) is not None:
            _write_json(path, config)
