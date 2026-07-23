from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO


_ALLOWED_FIELDS = frozenset(
    {
        "adapter",
        "bytes_read",
        "corpus_ids",
        "diagnostic_code",
        "duration_ms",
        "episode_count",
        "episode_ref",
        "episode_refs",
        "exception_class",
        "index_standing",
        "member_id",
        "operation_id",
        "outcome",
        "provider",
        "returned_count",
        "source_id",
        "source_standing",
        "standing",
        "work_exhausted",
    }
)


def event_log_path(
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    environ = os.environ if environ is None else environ
    configured = environ.get("LLM_MEMORY_EVENT_LOG")
    if configured:
        return Path(configured)
    return (Path.home() if home is None else home) / ".local/state/llm-memory/events.jsonl"


def emit_event(
    event: str,
    fields: Mapping[str, object],
    *,
    path: Path | None = None,
    stderr: TextIO | None = None,
) -> bool:
    if not isinstance(event, str) or not event:
        raise ValueError("event must be a nonempty string")
    unknown = set(fields) - _ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"unsupported operational event field: {sorted(unknown)[0]}")
    if any(_is_nested(value) for value in fields.values()):
        raise ValueError("event field values must not contain nested content")

    record = {
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "event": event,
        **fields,
    }
    target = event_log_path() if path is None else path
    stderr = sys.stderr if stderr is None else stderr
    try:
        encoded = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, encoded)
        finally:
            os.close(fd)
    except (OSError, TypeError) as exc:
        try:
            stderr.write(f"operational event write failed: {type(exc).__name__}\n")
        except OSError:
            pass
        return False
    return True


def _is_nested(value: object) -> bool:
    if isinstance(value, Mapping):
        return True
    if isinstance(value, (list, tuple)):
        return any(isinstance(item, (Mapping, list, tuple)) for item in value)
    return False
