"""Content-free operational events for the episodic store (spec D2,
principle 2). An event carries identifiers, digests, and counts only —
never query text, snippets, or episode bodies. Events are appended as JSON
lines to ~/.local/state/llm-memory/events.jsonl (LLM_MEMORY_EVENT_LOG
overrides the path). Writing never raises: an event that cannot be written
is dropped with a note on stderr, because the store's work must not fail on
its own bookkeeping. What the log is for: seeing that searches happen, how
wide their candidate sets are, and whether the same query digest recurs —
the material for improving search without reading anyone's words.

The query digest is keyed with a random per-machine secret kept beside the
log (`event-key`, 0600): an unkeyed hash of a short query can be reversed by
hashing guesses. The same query on the same machine still gives the same
digest, which is all the log needs.
"""

import fcntl
import hashlib
import hmac
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_MAX_RECORD_BYTES = 8192


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _query_digest(text):
    """HMAC-SHA256 of the query under this machine's event key, created on
    first use. None if the key cannot be read or made: bookkeeping must not
    fail a search."""
    try:
        path = _event_log_path().with_name("event-key")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = path.with_name(f".event-key.{os.getpid()}")
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(32))
            try:
                os.link(tmp, path)  # atomic; a concurrent first use keeps the winner's key
            except FileExistsError:
                pass
            finally:
                tmp.unlink()
        return hmac.new(path.read_bytes(), text.encode("utf-8"), hashlib.sha256).hexdigest()
    except Exception:
        return None


def emit_search_event(*, query, scope, since, until, total, returned, keys) -> bool:
    """A search completed. The query is recorded only as a keyed digest."""
    return _write(
        "search.completed",
        {
            "query_hmac": _query_digest(query),
            "scope": scope,
            "since": since,
            "until": until,
            "total": total,
            "returned": returned,
            "keys_sha256": _sha256("\n".join(keys)),
        },
    )


def emit_recall_event(*, key, found) -> bool:
    return _write("recall.completed", {"key": key, "found": found})


def emit_ingest_event(*, kind, label, host, count, source_file) -> bool:
    """One ingest run finished: `kind` is the subcommand, `source_file` the
    canonical path (or the sweep root) — a path, not content."""
    return _write(
        "ingest.completed",
        {
            "kind": kind,
            "label": label,
            "host": host,
            "count": count,
            "source_file": str(source_file),
        },
    )


def _event_log_path() -> Path:
    configured = os.environ.get("LLM_MEMORY_EVENT_LOG")
    if configured:
        return Path(configured)
    return Path.home() / ".local/state/llm-memory/events.jsonl"


def _write(event, fields) -> bool:
    try:
        record = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "event": event,
            **fields,
        }
        encoded = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > _MAX_RECORD_BYTES:
            return False
        target = _event_log_path()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                os.write(fd, encoded)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except BaseException as exc:
        try:
            sys.stderr.write(f"operational event write failed: {type(exc).__name__}\n")
        except BaseException:
            pass
        return False
    return True
