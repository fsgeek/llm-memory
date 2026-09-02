import json
import re
import socket
from pathlib import Path

from llm_memory.adapters import turn_text
from llm_memory.index import EPISODES
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
    return turn_text(content)


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
