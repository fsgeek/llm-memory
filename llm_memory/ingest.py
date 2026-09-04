import json
import re
import socket
from pathlib import Path

from llm_memory.index import EPISODES
from llm_memory.observability import emit_ingest_event
from llm_memory.schema import flatten_state

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Existing labels that differ from the project directory name (spec D3).
_LABEL_OVERRIDES = {"yanantin": "yanantin_construction"}


def label_from_project_dir(name):
    """Derive the experiment label from a `~/.claude/projects/<name>` directory
    name. Strips the projects prefix, folds worktree dirs into their project,
    maps scratchpad-launched dirs to the project they were launched from, and
    normalizes dots to dashes so `wamason.com` and `wamason-com` agree."""
    m = re.search(r"-projects-(.+)$", name)
    if m:
        rest = m.group(1)
        rest = re.split(rf"-{_UUID}", rest)[0]  # scratchpad dirs
        rest = re.split(r"-+\.?(?:claude-)?worktrees-", rest)[0]  # worktrees
        rest = rest.replace(".", "-")
        return _LABEL_OVERRIDES.get(rest, rest)
    m = re.match(r"-home-[^-]+(.*)$", name)
    if m:
        rest = m.group(1).strip("-.").replace(".", "-")
        return f"home-{rest}" if rest else "home"
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def label_from_path(path):
    """Label for a working-directory path (a Codex rollout's `cwd`, Claude's
    CLAUDE_PROJECT_DIR), by the same rule as the project directory name:
    Claude Code names `~/.claude/projects/<name>` by replacing every
    non-alphanumeric character in the path with a dash."""
    return label_from_project_dir(re.sub(r"[^A-Za-z0-9]", "-", str(path)))


def record_to_episode(record, source_file):
    """Transform one taste_open cycle record into an episode document that carries
    BOTH sides of the conversation and the flattened state."""
    state = record.get("state") or {}
    raw = record.get("raw_output")
    response = raw.get("response") if isinstance(raw, dict) else None
    if not response:
        response = record.get("response_text", "")

    cycle = record["cycle"]
    return {
        "_key": f"{cycle:06d}",
        "cycle": cycle,
        "ts": record.get("timestamp"),
        "model": record.get("model"),
        "experiment_label": record.get("experiment_label"),
        "source_file": source_file,
        "user_message": record.get("user_message", "") or "",
        "response": response or "",
        "state": state,
        # exclude _activity_log (tool-trace) — it echoes the instance's own
        # queries and would confound recall if indexed as state.
        "state_text": flatten_state(
            {k: v for k, v in state.items() if k != "_activity_log"}
        ),
        "activity_log": state.get("_activity_log", []),
    }


def _turn_text(content):
    """Extract plain text from a message turn whose content is either a string or
    a list of Anthropic content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def gateway_record_to_episode(record, seq, source_file):
    """Transform one pichay gateway `request_metrics` event into an episode. The
    exchange's `user_message` is the LAST user turn in `messages_full` (the prompt
    this turn answered); `response` is `response_text` — the words pichay now
    captures instead of dropping. Claude Code has no authored state, so state is
    empty. Keyed by session+seq since there is no global cycle counter."""
    session = record.get("session_id") or "unknown"
    messages = record.get("messages_full") or []
    user_message = ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            user_message = _turn_text(m.get("content"))
            break
    return {
        "_key": f"{session}-{seq:04d}",
        "cycle": seq,
        "session_id": session,
        "ts": record.get("timestamp"),
        "model": record.get("model"),
        "experiment_label": "claude_code",
        "source_file": source_file,
        "user_message": user_message,
        "response": record.get("response_text", "") or "",
        "state": {},
        "state_text": "",
        "activity_log": [],
        "messages_full": messages,
    }


def read_machine_id(path=Path("/etc/machine-id")):
    """The 32-hex machine id, un-hyphenated, matching the existing episodes."""
    return path.read_text(encoding="utf-8").strip()


def claude_session_to_episodes(path, experiment_label, host=None, machine_id=None):
    """Yield one episode per assistant turn in a Claude Code project JSONL.

    Each line is one event; `type` is `user`/`assistant`/etc. An episode pairs an
    assistant turn (`response`) with the most recent preceding user turn
    (`user_message`), mirroring the gateway mapper. Claude Code has no authored
    state, so state is empty. Keyed by the assistant uuid alone (amendment A4):
    fork/resume copies a session's history into a new file under a new
    sessionId but keeps the uuid, so the uuid is the identity of the message.
    `experiment_label` is caller-supplied so a project's construction history
    partitions distinctly from pichay-captured `claude_code` traffic. `host` and
    `machine_id` are the machine of origin (spec D3); the caller supplies them
    because a staged copy on another machine must not claim to be local."""
    session = "unknown"
    last_user = ""
    last_user_ts = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            session = rec.get("sessionId") or session
            if rtype == "user":
                last_user = _turn_text(msg.get("content"))
                last_user_ts = rec.get("timestamp")
                continue
            if rtype != "assistant":
                continue
            response = _turn_text(msg.get("content"))
            if not response.strip():
                continue  # tool-use-only turn with no prose; skip
            uuid = rec.get("uuid") or ""
            yield {
                "_key": uuid,
                "session_id": session,
                "ts": rec.get("timestamp"),
                "model": msg.get("model"),
                "experiment_label": experiment_label,
                "source_file": str(path),
                "user_message": last_user,
                "user_ts": last_user_ts,
                "response": response,
                "state": {},
                "state_text": "",
                "activity_log": [],
                "host": host,
                "machine_id": machine_id,
                "agent_id": rec.get("agentId"),
            }


def claude_session_files(path):
    """A Claude Code session is its project JSONL plus any subagent transcripts
    under `<session-uuid>/subagents/*.jsonl` beside it (spec D3)."""
    path = Path(path)
    subagents = path.with_suffix("") / "subagents"
    return [path] + sorted(subagents.glob("*.jsonl"))


def ingest_claude_session(db, path, experiment_label, dry_run=False, host=None, machine_id=None):
    """Load one Claude Code session (project JSONL plus its subagent
    transcripts) into the episodes collection. One episode per prose assistant
    turn. Idempotent per assistant uuid. When dry_run, counts what WOULD be
    inserted without writing. Returns the count."""
    col = db.collection(EPISODES)
    count = 0
    for file in claude_session_files(path):
        for episode in claude_session_to_episodes(file, experiment_label, host=host, machine_id=machine_id):
            if not dry_run:
                col.insert(episode, overwrite=True)
            count += 1
    return count


def ingest_gateway_file(db, path):
    """Load a pichay gateway log (jsonl of telemetry events) into the episodes
    collection. Only `request_metrics` events become episodes, sequenced per
    session. Idempotent per (session, seq). Returns the number ingested."""
    col = db.collection(EPISODES)
    source = str(path)
    seq_by_session = {}
    count = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("type") != "request_metrics":
                continue
            session = rec.get("session_id") or "unknown"
            seq = seq_by_session.get(session, 0)
            seq_by_session[session] = seq + 1
            episode = gateway_record_to_episode(rec, seq=seq, source_file=source)
            col.insert(episode, overwrite=True)
            count += 1
    return count


def ingest_file(db, path):
    """Load a taste_open jsonl into the episodes collection. Idempotent per cycle
    (overwrite by _key). Returns the number of records ingested."""
    col = db.collection(EPISODES)
    source = str(path)
    count = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            episode = record_to_episode(json.loads(line), source_file=source)
            col.insert(episode, overwrite=True)
            count += 1
    return count


# User-role messages the Codex harness injects; not the human's (or Claude's)
# prompt. See docs/findings-2026-09-02-codex-rollout-format.md.
_CODEX_INJECTED_TAGS = {
    "environment_context", "codex_internal_context", "recommended_plugins",
    "turn_aborted", "user_instructions", "subagent_notification",
}
_CODEX_INJECTED_PREFIXES = (
    "# AGENTS.md instructions for",
    "Warning: apply_patch was requested via",
)


def _codex_text(content):
    if isinstance(content, str):
        return content
    return "".join(
        c.get("text", "") for c in (content or []) if isinstance(c, dict)
    )


def _codex_injected(text):
    t = text.lstrip()
    if t.startswith(_CODEX_INJECTED_PREFIXES):
        return True
    m = re.match(r"<([A-Za-z_]+)", t)
    return bool(m) and m.group(1) in _CODEX_INJECTED_TAGS


def _iso(s):
    from datetime import datetime

    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def codex_rollout_to_episodes(path, experiment_label=None, host=None, machine_id=None):
    """Yield one episode per prose assistant message in a Codex CLI rollout
    JSONL (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`), paired with the
    most recent preceding user prompt — the mirror of
    `claude_session_to_episodes`. Built only on the layers present in every
    observed cli_version (0.0.0 .. 0.151.0): `session_meta` (first one wins;
    forked files carry the parent's second), `turn_context.model`, and
    `response_item` messages with role user/assistant. Never reads
    `event_msg` (absent in 0.147+) or `compacted.replacement_history`
    (replays prompts). Injected user-role context is not a prompt; a
    subagent's task arrives encrypted, so its episodes have an empty
    `user_message` — nobody's words are recoverable, and the field says so.
    The label defaults to the project the rollout's `cwd` was in, so a
    project-scoped search returns what Codex said there; `codex.originator`
    records who the "user" was (Tony, a Claude instance, or a parent agent)."""
    meta = None
    meta_ts = None
    model = None
    last_user, last_user_ts = "", None
    # A forked rollout (session_meta.forked_from_id) begins by replaying the
    # parent's history, all stamped within ~0.1s of the fork's own timestamp;
    # the first live turn_context comes seconds later. Those rows are already
    # episodes of the parent session, so skip them.
    in_replay = False
    with open(path) as f:
        for lineno, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            p = rec.get("payload") or {}
            if rtype == "session_meta":
                if meta is None:
                    meta = p
                    meta_ts = _iso(rec["timestamp"])
                    in_replay = bool(p.get("forked_from_id"))
                continue
            if rtype == "turn_context":
                model = p.get("model") or model
                if in_replay and (_iso(rec["timestamp"]) - meta_ts).total_seconds() > 1:
                    in_replay = False
                continue
            if in_replay:
                continue
            if rtype != "response_item" or p.get("type") != "message":
                continue
            role = p.get("role")
            text = _codex_text(p.get("content"))
            if role == "user":
                if _codex_injected(text):
                    continue
                last_user, last_user_ts = text, rec.get("timestamp")
                continue
            if role != "assistant" or not text.strip():
                continue
            meta = meta or {}
            session = meta.get("id") or "unknown"
            msg_id = p.get("id") or f"line{lineno}"
            cwd = meta.get("cwd")
            yield {
                "_key": f"{session}-{msg_id}",
                "session_id": session,
                "ts": rec.get("timestamp"),
                "model": model,
                "experiment_label": experiment_label
                or (label_from_path(cwd) if cwd else "codex"),
                "source_file": str(path),
                "user_message": last_user,
                "user_ts": last_user_ts,
                "response": text,
                "state": {},
                "state_text": "",
                "activity_log": [],
                "host": host,
                "machine_id": machine_id,
                "codex": {
                    "originator": meta.get("originator"),
                    "cli_version": meta.get("cli_version"),
                    "cwd": cwd,
                    "thread_source": meta.get("thread_source"),
                    "agent_nickname": meta.get("agent_nickname"),
                    "forked_from_id": meta.get("forked_from_id"),
                },
            }


def codex_rollout_files(root):
    """Every rollout file under a Codex sessions tree, oldest first."""
    return sorted(Path(root).glob("*/*/*/rollout-*.jsonl"))


def ingest_codex_rollout(db, path, experiment_label=None, dry_run=False, host=None, machine_id=None):
    """Load one Codex rollout into the episodes collection. Idempotent per
    (session, message id). Returns the count."""
    col = db.collection(EPISODES)
    count = 0
    for episode in codex_rollout_to_episodes(path, experiment_label, host=host, machine_id=machine_id):
        if not dry_run:
            col.insert(episode, overwrite=True)
        count += 1
    return count


def main(argv=None):
    """`python -m llm_memory.ingest claude-session [PATH]`. Without PATH, reads
    the Claude Code hook JSON from stdin and ingests its `transcript_path`
    (SessionEnd hook mode, spec D4). Fails loudly and does not retry: a
    traceback on stderr and a non-zero exit are the contract; the sweep is
    the retry."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m llm_memory.ingest")
    sub = parser.add_subparsers(dest="command", required=True)
    cs = sub.add_parser("claude-session", help="ingest one Claude Code session (path or hook JSON on stdin)")
    cs.add_argument("path", nargs="?", help="project JSONL; omit to read hook JSON from stdin")
    cs.add_argument("--label", help="experiment label (default: derived from the project directory name)")
    cs.add_argument("--host", help="originating hostname (default: this machine)")
    cs.add_argument("--machine-id", help="originating /etc/machine-id (default: this machine)")
    cs.add_argument("--dry-run", action="store_true", help="count without writing")
    cx = sub.add_parser("codex", help="ingest Codex CLI rollout files (one path, --all under --root, or hook JSON on stdin)")
    cx.add_argument("path", nargs="?", help="one rollout JSONL; omit (without --all) to read Codex hook JSON from stdin")
    cx.add_argument("--all", action="store_true", help="every rollout under --root")
    cx.add_argument("--root", default=Path.home() / ".codex" / "sessions", help="Codex sessions tree (default: ~/.codex/sessions)")
    cx.add_argument("--label", help="experiment label (default: the project of each rollout's cwd)")
    cx.add_argument("--host", help="originating hostname (default: this machine)")
    cx.add_argument("--machine-id", help="originating /etc/machine-id (default: this machine)")
    cx.add_argument("--dry-run", action="store_true", help="count without writing")
    args = parser.parse_args(argv)

    if args.command == "codex":
        return _main_codex(args)

    if args.path:
        path = Path(args.path)
    else:
        hook = json.load(sys.stdin)
        path = Path(hook["transcript_path"])
    if not path.is_file():
        print(f"claude-session: transcript not found: {path}", file=sys.stderr)
        return 2
    label = args.label or label_from_project_dir(path.parent.name)
    host = args.host or socket.gethostname()
    machine_id = args.machine_id or read_machine_id()

    from llm_memory.db import get_database

    db = get_database()
    count = ingest_claude_session(db, path, label, dry_run=args.dry_run, host=host, machine_id=machine_id)
    verb = "would ingest" if args.dry_run else "ingested"
    if not args.dry_run:
        emit_ingest_event(kind="claude-session", label=label, host=host, count=count, source_file=path)
    print(f"claude-session: {verb} {count} episodes from {path} (label={label}, host={host})")
    return 0


def _main_codex(args):
    import sys

    if args.path and args.all:
        print("codex: give PATH or --all, not both", file=sys.stderr)
        return 2
    if args.all:
        files = codex_rollout_files(args.root)
    elif args.path:
        files = [Path(args.path)]
    else:
        # Codex hook mode (SessionEnd / SubagentStop): the hook JSON names the
        # rollout that just closed. Same contract as the Claude hook.
        hook = json.load(sys.stdin)
        files = [Path(hook.get("agent_transcript_path") or hook["transcript_path"])]
    missing = [f for f in files if not f.is_file()]
    if missing:
        print(f"codex: rollout not found: {missing[0]}", file=sys.stderr)
        return 2
    host = args.host or socket.gethostname()
    machine_id = args.machine_id or read_machine_id()

    from llm_memory.db import get_database

    db = get_database()
    count = 0
    for file in files:
        count += ingest_codex_rollout(db, file, args.label, dry_run=args.dry_run, host=host, machine_id=machine_id)
    verb = "would ingest" if args.dry_run else "ingested"
    where = f"{len(files)} files under {args.root}" if args.all else files[0]
    if not args.dry_run:
        emit_ingest_event(kind="codex", label=args.label, host=host, count=count, source_file=args.root if args.all else files[0])
    print(f"codex: {verb} {count} episodes from {where} (host={host})")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
