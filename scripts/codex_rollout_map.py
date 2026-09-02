"""Prototype (2026-09-02, see docs/findings-2026-09-02-codex-rollout-format.md):
Codex CLI rollout JSONL -> llm-memory episodes. Not wired into ingest yet.

Mirrors claude_session_to_episodes: one episode per prose assistant message,
paired with the most recent preceding user prompt. Built only on the layers
present in every observed cli_version (0.0.0 .. 0.151.0):
  - session_meta   (first one wins; forked files carry the parent's second)
  - turn_context   (payload.model, present in all versions)
  - response_item  (payload.type == "message", role user/assistant)
Never reads event_msg (agent_message/user_message vanish in 0.147+) or
compacted.replacement_history (replays prompts; would duplicate).
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# User-role messages the harness injects; not the human's (or Claude's) prompt.
INJECTED_TAGS = {
    "environment_context", "codex_internal_context", "recommended_plugins",
    "turn_aborted", "user_instructions", "subagent_notification",
}
INJECTED_PREFIXES = (
    "# AGENTS.md instructions for",
    "Warning: apply_patch was requested via",
)


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(
        c.get("text", "") for c in (content or []) if isinstance(c, dict)
    )


def _is_injected(text):
    t = text.lstrip()
    if t.startswith(INJECTED_PREFIXES):
        return True
    m = re.match(r"<([A-Za-z_]+)", t)
    return bool(m) and m.group(1) in INJECTED_TAGS


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def codex_rollout_to_episodes(path, experiment_label):
    meta = None
    meta_ts = None
    model = None
    last_user, last_user_ts = "", None
    # A forked rollout (session_meta.forked_from_id) begins by replaying the
    # parent's history, all stamped within ~0.1s of the fork's own timestamp;
    # the first live turn_context comes seconds later. Those replayed rows are
    # already episodes of the parent session, so skip them.
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
                    meta_ts = _ts(rec["timestamp"])
                    in_replay = bool(p.get("forked_from_id"))
                continue
            if rtype == "turn_context":
                model = p.get("model") or model
                if in_replay and meta_ts and (_ts(rec["timestamp"]) - meta_ts).total_seconds() > 1:
                    in_replay = False
                continue
            if in_replay:
                continue
            if rtype != "response_item" or p.get("type") != "message":
                continue
            role = p.get("role")
            text = _text(p.get("content"))
            if role == "user":
                if _is_injected(text):
                    continue
                last_user, last_user_ts = text, rec.get("timestamp")
                continue
            if role != "assistant" or not text.strip():
                continue
            session = (meta or {}).get("id") or "unknown"
            msg_id = p.get("id") or f"line{lineno}"
            yield {
                "_key": f"{session}-{msg_id}",
                "session_id": session,
                "ts": rec.get("timestamp"),
                "model": model,
                "experiment_label": experiment_label,
                "source_file": str(path),
                "user_message": last_user,
                "user_ts": last_user_ts,
                "response": text,
                "state": {},
                "state_text": "",
                "activity_log": [],
                # Codex-specific provenance (all from the first session_meta).
                "codex": {
                    "originator": (meta or {}).get("originator"),
                    "cli_version": (meta or {}).get("cli_version"),
                    "cwd": (meta or {}).get("cwd"),
                    "thread_source": (meta or {}).get("thread_source"),
                    "agent_nickname": (meta or {}).get("agent_nickname"),
                    "forked_from_id": (meta or {}).get("forked_from_id"),
                },
            }


if __name__ == "__main__":
    for e in codex_rollout_to_episodes(Path(sys.argv[1]), "codex"):
        print(e["ts"][11:19], e["model"], "|",
              repr(e["user_message"][:50]), "->", repr(e["response"][:60]))
