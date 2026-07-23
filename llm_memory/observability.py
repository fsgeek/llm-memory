from __future__ import annotations

import fcntl
import json
import math
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from llm_memory.contract import (
    EpisodeReference,
    IndexStanding,
    OpenStanding,
    SourceStanding,
    validate_corpus_id,
)


_ALLOWED_EVENTS = frozenset(
    {
        "server.starting",
        "server.started",
        "server.stopped",
        "server.failed",
        "reconcile.completed",
        "search.completed",
        "search.failed",
        "open.completed",
        "open.failed",
    }
)
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_CLASS_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_IDENTIFIER_LENGTH = 128
_MAX_EPISODE_REF_LENGTH = 2048
_MAX_LIST_ITEMS = 100
_MAX_RECORD_BYTES = 8192
_DIAGNOSTIC_CODES = frozenset(
    {"server_startup_failed", "contract_search_failed", "contract_open_failed"}
)
_OUTCOMES = frozenset({"completed", "enrollment_missing"})
_STANDINGS = frozenset((*OpenStanding, "unknown"))
_SOURCE_STANDINGS = frozenset(SourceStanding)
_INDEX_STANDINGS = frozenset(IndexStanding)


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
    _validate_event(event, fields)
    record = {
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "event": event,
        **fields,
    }
    encoded = _encode_record(record)
    target = event_log_path() if path is None else path
    stderr = sys.stderr if stderr is None else stderr
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                prior_end = os.lseek(fd, 0, os.SEEK_END)
                written = os.write(fd, encoded)
                if written != len(encoded):
                    os.ftruncate(fd, prior_end)
                    raise OSError("operational event short write")
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except OSError as exc:
        _report_failure(stderr, exc)
        return False
    return True


def _validate_event(event: str, fields: Mapping[str, object]) -> None:
    if not isinstance(event, str) or event not in _ALLOWED_EVENTS:
        raise ValueError("unsupported operational event")
    if not isinstance(fields, Mapping):
        raise ValueError("event fields must be a mapping")
    unknown = set(fields) - set(_FIELD_VALIDATORS)
    if unknown:
        raise ValueError(f"unsupported operational event field: {next(iter(unknown))}")
    for name, value in fields.items():
        _FIELD_VALIDATORS[name](value, name)


def _encode_record(record: dict[str, object]) -> bytes:
    encoded = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > _MAX_RECORD_BYTES:
        raise ValueError("operational event record is too large")
    return encoded


def _validate_identifier(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_IDENTIFIER_LENGTH
        or _IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        _invalid(name)


def _validate_class_name(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_IDENTIFIER_LENGTH
        or _CLASS_NAME_PATTERN.fullmatch(value) is None
    ):
        _invalid(name)


def _validate_corpus_ids(value: object, name: str) -> None:
    if not isinstance(value, list) or len(value) > _MAX_LIST_ITEMS:
        _invalid(name)
    for corpus_id in value:
        if not isinstance(corpus_id, str) or len(corpus_id) > _MAX_IDENTIFIER_LENGTH:
            _invalid(name)
        try:
            validate_corpus_id(corpus_id)
        except ValueError:
            _invalid(name)


def _validate_episode_ref(value: object, name: str) -> None:
    if not isinstance(value, str) or len(value) > _MAX_EPISODE_REF_LENGTH:
        _invalid(name)
    try:
        EpisodeReference.parse(value)
    except ValueError:
        _invalid(name)


def _validate_episode_refs(value: object, name: str) -> None:
    if not isinstance(value, list) or len(value) > _MAX_LIST_ITEMS:
        _invalid(name)
    for episode_ref in value:
        _validate_episode_ref(episode_ref, name)


def _validate_enum(values: frozenset[str]) -> Callable[[object, str], None]:
    def validate(value: object, name: str) -> None:
        if not isinstance(value, str) or value not in values:
            _invalid(name)

    return validate


def _validate_count(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _invalid(name)


def _validate_duration(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or not math.isfinite(value)
    ):
        _invalid(name)


def _validate_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        _invalid(name)


def _invalid(name: str) -> None:
    raise ValueError(f"invalid operational event field: {name}")


_FIELD_VALIDATORS: Mapping[str, Callable[[object, str], None]] = {
    "adapter": _validate_identifier,
    "bytes_read": _validate_count,
    "corpus_ids": _validate_corpus_ids,
    "diagnostic_code": _validate_enum(_DIAGNOSTIC_CODES),
    "duration_ms": _validate_duration,
    "episode_count": _validate_count,
    "episode_ref": _validate_episode_ref,
    "episode_refs": _validate_episode_refs,
    "exception_class": _validate_class_name,
    "index_standing": _validate_enum(_INDEX_STANDINGS),
    "member_id": _validate_identifier,
    "operation_id": _validate_identifier,
    "outcome": _validate_enum(_OUTCOMES),
    "provider": _validate_identifier,
    "returned_count": _validate_count,
    "source_id": _validate_identifier,
    "source_standing": _validate_enum(_SOURCE_STANDINGS),
    "standing": _validate_enum(_STANDINGS),
    "work_exhausted": _validate_bool,
}


def _report_failure(stderr: TextIO, exc: OSError) -> None:
    try:
        stderr.write(f"operational event write failed: {type(exc).__name__}\n")
    except Exception:
        pass
