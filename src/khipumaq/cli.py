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
    _sweep_windows(get_database(), since, host)
    SWEEP_STATE.parent.mkdir(parents=True, exist_ok=True)
    SWEEP_STATE.write_text(str(started))
    return 0


def _sweep_windows(db, since, host):
    """On WSL, also sweep the Windows user's Claude Code, Cowork, and Codex
    transcripts: Windows runs no hooks of ours, so this is their only path in."""
    from khipumaq import wsl
    from khipumaq.ingest import sweep
    from khipumaq.observability import emit_ingest_event

    profile = wsl.windows_profile()
    if profile is None:
        return
    machine_id = wsl.windows_machine_id()
    totals = {}
    for claude_root, codex_root, label in wsl.sources(profile):
        result = sweep(db, claude_root, codex_root, since=since, host=host, machine_id=machine_id,
                       canonical=wsl.windows_path, label=label)
        for kind, (files, count) in result.items():
            name = label or f"windows-{kind}"
            f0, c0 = totals.get(name, (0, 0))
            totals[name] = (f0 + files, c0 + count)
    for name, (files, count) in totals.items():
        emit_ingest_event(kind=f"sweep-{name}", label=None, host=host, count=count,
                          source_file=wsl.windows_path(profile))
        print(f"sweep: {name}: {count} episodes from {files} files under {wsl.windows_path(profile)}")


def _claude_paths():
    home = Path.home()
    return home / ".claude" / "settings.json", home / ".claude.json"


def _install(skip_codex):
    from khipumaq import setup
    from khipumaq.db import config_path

    status = 0
    setup.install_claude(*_claude_paths())
    print("khipumaq: Claude Code SessionEnd hook and MCP server installed.")
    if skip_codex:
        pass
    elif not setup.codex_home().is_dir():
        print(f"khipumaq: no Codex home at {setup.codex_home()}; Codex hooks skipped.")
    elif (codex_bin := setup.codex_binary()) is None:
        print("khipumaq: codex binary not found (set CODEX_BIN); Codex hooks skipped.", file=sys.stderr)
    else:
        try:
            setup.install_codex(setup.codex_home(), codex_bin)
            print("khipumaq: Codex SessionEnd/SubagentStop hooks installed and trusted.")
        except RuntimeError as exc:
            # The hooks are written but untrusted, so Codex will not run them.
            print(f"khipumaq: Codex hooks NOT trusted: {exc}", file=sys.stderr)
            status = 1
    from khipumaq.wsl import windows_path, windows_profile

    if (profile := windows_profile()) is not None:
        for path in setup.install_windows_clients(profile):
            print(f"khipumaq: MCP server registered for Windows in {windows_path(path)} (via wsl.exe).")
    if setup.has_systemd_user():
        setup.install_timer()
        print("khipumaq: nightly sweep timer enabled (systemctl --user status khipumaq-sweep.timer).")
    else:
        print("khipumaq: no systemd user session; schedule `khipumaq sweep` yourself.", file=sys.stderr)
    try:
        print(f"khipumaq: database config: {config_path()}")
    except FileNotFoundError as exc:
        print(f"khipumaq: {exc}\n          Hooks and server will fail until it exists.", file=sys.stderr)
        status = 1
    else:
        status = _ensure_store() or status
    print("khipumaq: restart Claude Code for the MCP server to load.")
    return status


def _ensure_store():
    """Create the episodes collection and its search view on a fresh database;
    an existing store is left as it is."""
    from khipumaq.db import get_database
    from khipumaq.index import EPISODES, ensure_index

    try:
        db = get_database()
        if db.has_collection(EPISODES):
            print(f"khipumaq: store reachable, {db.collection(EPISODES).count():,} episodes.")
        else:
            ensure_index(db)
            print("khipumaq: store created (episodes collection and search view).")
    except Exception as exc:  # noqa: BLE001 — any failure here is worth naming
        print(f"khipumaq: database unreachable ({type(exc).__name__}: {exc}).", file=sys.stderr)
        return 1
    return 0


def _uninstall():
    from khipumaq import setup

    setup.uninstall_claude(*_claude_paths())
    setup.uninstall_codex(setup.codex_home())
    setup.uninstall_timer()
    from khipumaq.wsl import windows_profile

    if (profile := windows_profile()) is not None:
        setup.uninstall_windows_clients(profile)
    print("khipumaq: hooks, MCP entry, and sweep timer removed. The store is untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
