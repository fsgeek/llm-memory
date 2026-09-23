"""Entry point for the `khipumaq` command.

Imports are deferred to the subcommand that needs them: `codex-hook` must
detach within Codex's one-second hook budget, so it cannot pay for the MCP or
Arango imports it never uses.
"""

import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="khipumaq")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the read-only MCP server (stdio)")
    ingest = sub.add_parser("ingest", help="ingest sessions (see `khipumaq ingest -h`)", add_help=False)
    ingest.add_argument("rest", nargs=argparse.REMAINDER)
    sub.add_parser("describe", help="print what the store holds")
    sweep = sub.add_parser("sweep", help="ingest this machine's sessions changed since the last sweep")
    sweep.add_argument("--all", action="store_true", help="ignore the last sweep; ingest every session file")
    install = sub.add_parser("install", help="wire hooks and the MCP server on this machine")
    install.add_argument("--no-codex", action="store_true", help="skip the Codex hooks")
    sub.add_parser("uninstall", help="remove khipumaq's hooks and MCP entry")
    sub.add_parser("codex-hook", help="(run by Codex) detach an ingest of the closing rollout")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from khipumaq.mcp_server import mcp

        mcp.run()
        return 0
    if args.command == "ingest":
        from khipumaq.ingest import main as ingest_main

        return ingest_main(args.rest)
    if args.command == "describe":
        from khipumaq.db import get_database
        from khipumaq.describe import describe

        print(json.dumps(describe(get_database()), indent=2))
        return 0
    if args.command == "sweep":
        return _sweep(everything=args.all)
    if args.command == "codex-hook":
        from khipumaq.setup import codex_hook

        return codex_hook()
    if args.command == "install":
        return _install(skip_codex=args.no_codex)
    return _uninstall()


SWEEP_STATE = Path.home() / ".local" / "state" / "khipumaq" / "last-sweep"
SWEEP_MARGIN = 86400  # re-read a day before the last sweep: sessions still open then


def _sweep(everything):
    """Ingest session files modified since the last successful sweep, so a
    failed hook is caught the next time this runs (spec D4 layer 2). The first
    run, or --all, reads everything still on disk."""
    import socket
    import time

    from khipumaq.db import get_database
    from khipumaq.ingest import read_machine_id, sweep
    from khipumaq.observability import emit_ingest_event
    from khipumaq.setup import codex_home

    started = time.time()
    since = None
    if not everything and SWEEP_STATE.exists():
        since = float(SWEEP_STATE.read_text()) - SWEEP_MARGIN
    host, claude_root = socket.gethostname(), Path.home() / ".claude" / "projects"
    result = sweep(get_database(), claude_root, codex_home() / "sessions", since=since,
                   host=host, machine_id=read_machine_id())
    for kind, root in (("claude", claude_root), ("codex", codex_home() / "sessions")):
        files, count = result[kind]
        emit_ingest_event(kind=f"sweep-{kind}", label=None, host=host, count=count, source_file=root)
        print(f"sweep: {kind}: {count} episodes from {files} files under {root}")
    SWEEP_STATE.parent.mkdir(parents=True, exist_ok=True)
    SWEEP_STATE.write_text(str(started))
    return 0


def _claude_paths():
    home = Path.home()
    return home / ".claude" / "settings.json", home / ".claude.json"


def _install(skip_codex):
    from khipumaq import setup
    from khipumaq.db import config_path

    setup.install_claude(*_claude_paths())
    print("khipumaq: Claude Code SessionEnd hook and MCP server installed.")
    if skip_codex:
        pass
    elif not setup.codex_home().is_dir():
        print(f"khipumaq: no Codex home at {setup.codex_home()}; Codex hooks skipped.")
    elif (codex_bin := setup.codex_binary()) is None:
        print("khipumaq: codex binary not found (set CODEX_BIN); Codex hooks skipped.", file=sys.stderr)
    else:
        setup.install_codex(setup.codex_home(), codex_bin)
        print("khipumaq: Codex SessionEnd/SubagentStop hooks installed and trusted.")
    if setup.has_systemd_user():
        setup.install_timer()
        print("khipumaq: nightly sweep timer enabled (systemctl --user status khipumaq-sweep.timer).")
    else:
        print("khipumaq: no systemd user session; schedule `khipumaq sweep` yourself.", file=sys.stderr)
    try:
        print(f"khipumaq: database config: {config_path()}")
    except FileNotFoundError as exc:
        print(f"khipumaq: {exc}\n          Hooks and server will fail until it exists.", file=sys.stderr)
    print("khipumaq: restart Claude Code for the MCP server to load.")
    return 0


def _uninstall():
    from khipumaq import setup

    setup.uninstall_claude(*_claude_paths())
    setup.uninstall_codex(setup.codex_home())
    setup.uninstall_timer()
    print("khipumaq: hooks, MCP entry, and sweep timer removed. The store is untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
