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
    if args.command == "codex-hook":
        from khipumaq.setup import codex_hook

        return codex_hook()
    if args.command == "install":
        return _install(skip_codex=args.no_codex)
    return _uninstall()


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
    print("khipumaq: hooks and MCP entry removed. The store is untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
