from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from llm_memory.contract import (
    EpisodeReference,
    IndexStanding,
    OpenStanding,
    SourceStanding,
    validate_corpus_id,
)
from llm_memory.machine_identity import normalize_machine_uuid


_MAX_CORPORA = 100
_MAX_EPISODE_REFS = 100
_MAX_EPISODE_REF_LENGTH = 2048
_MAX_OPAQUE_IDENTIFIER_LENGTH = 4096
_MAX_RECORD_BYTES = 8192
_CLASS_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SERVER_STATES = frozenset({"starting", "started", "stopped"})
_OUTCOMES = frozenset({"completed", "enrollment_missing"})
_INITIALIZATION_OUTCOMES = {
    "provider": frozenset({"initialized"}),
    "enrollment": frozenset({"initialized", "missing"}),
}
_SOURCE_STANDINGS = frozenset(SourceStanding)
_INDEX_STANDINGS = frozenset(IndexStanding)
_OPEN_STANDINGS = frozenset((*OpenStanding, "unknown"))
_FAILURE_CODES = {
    "server": "server_startup_failed",
    "provider": "provider_initialization_failed",
    "enrollment": "enrollment_initialization_failed",
    "reconciliation": "reconciliation_failed",
    "search": "contract_search_failed",
    "open": "contract_open_failed",
}


def emit_server_event(state, *, outcome=None) -> bool:
    try:
        if state not in _SERVER_STATES:
            return False
        fields = {} if outcome is None else {"outcome": _required_enum(outcome, _OUTCOMES)}
        return _write_envelope(f"server.{state}", fields)
    except BaseException:
        return False


def emit_initialization_event(component, *, outcome) -> bool:
    try:
        allowed_outcomes = _INITIALIZATION_OUTCOMES.get(component)
        if allowed_outcomes is None:
            return False
        valid_outcome = _required_enum(outcome, allowed_outcomes)
        return _write_envelope(
            f"{component}.initialized", {"outcome": valid_outcome}
        )
    except BaseException:
        return False


def emit_reconciliation_started(*, corpus_count, source_count) -> bool:
    try:
        return _write_envelope(
            "reconcile.started",
            {
                "corpus_count": _nonnegative_int(corpus_count),
                "source_count": _nonnegative_int(source_count),
            },
        )
    except BaseException:
        return False


def emit_reconciliation_event(
    *,
    corpus_id,
    source_id,
    member_id,
    source_standing,
    index_standing,
    episode_count,
    bytes_read,
    duration_ms,
    work_exhausted,
) -> bool:
    try:
        return _write_envelope(
            "reconcile.completed",
            {
                "corpus_ids": [_valid_corpus_id(corpus_id)],
                "source_id": _correlation_token(source_id),
                "member_id": _correlation_token(member_id),
                "source_standing": _required_enum(source_standing, _SOURCE_STANDINGS),
                "index_standing": _required_enum(index_standing, _INDEX_STANDINGS),
                "episode_count": _nonnegative_int(episode_count),
                "bytes_read": _nonnegative_int(bytes_read),
                "duration_ms": _duration(duration_ms),
                "work_exhausted": _bool(work_exhausted),
            },
        )
    except BaseException:
        return False


def emit_search_event(*, corpus_ids, returned_count, episode_refs) -> bool:
    try:
        valid_refs = _episode_refs(episode_refs)
        return _write_envelope(
            "search.completed",
            {
                "corpus_ids": _corpus_ids(corpus_ids),
                "returned_count": _nonnegative_int(returned_count),
                "episode_refs_sha256": hashlib.sha256(
                    "\n".join(valid_refs).encode("utf-8")
                ).hexdigest(),
            },
        )
    except BaseException:
        return False


def emit_open_event(*, corpus_ids, episode_ref, standing) -> bool:
    try:
        return _write_envelope(
            "open.completed",
            {
                "corpus_ids": _corpus_ids(corpus_ids),
                "episode_ref": _episode_ref(episode_ref),
                "standing": _required_enum(standing, _OPEN_STANDINGS),
            },
        )
    except BaseException:
        return False


def emit_failure_event(phase, exc, *, corpus_ids=(), episode_ref=None) -> bool:
    try:
        if phase not in _FAILURE_CODES or not isinstance(exc, BaseException):
            return False
        exception_class = type(exc).__name__
        if _CLASS_NAME_PATTERN.fullmatch(exception_class) is None:
            return False
        fields = {
            "exception_class": exception_class,
            "diagnostic_code": _FAILURE_CODES[phase],
        }
        valid_corpora = _optional_corpus_ids(corpus_ids)
        if valid_corpora:
            fields["corpus_ids"] = valid_corpora
        valid_ref = _optional_episode_ref(episode_ref)
        if valid_ref is not None:
            fields["episode_ref"] = valid_ref
        return _write_envelope(f"{phase}.failed", fields)
    except BaseException:
        return False


def _event_log_path() -> Path:
    configured = os.environ.get("LLM_MEMORY_EVENT_LOG")
    if configured:
        return Path(configured)
    return Path.home() / ".local/state/llm-memory/events.jsonl"


def _write_envelope(event: str, fields: dict[str, object]) -> bool:
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
    except BaseException as exc:
        _report_failure(exc)
        return False
    return True


def _valid_corpus_id(value) -> str:
    if not isinstance(value, str):
        raise ValueError("corpus identifier must be text")
    return validate_corpus_id(value)


def _corpus_ids(values) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > _MAX_CORPORA:
        raise ValueError("corpus identifiers must be bounded")
    return [_valid_corpus_id(value) for value in values]


def _optional_corpus_ids(values) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > _MAX_CORPORA:
        return []
    valid = []
    for value in values:
        try:
            valid.append(_valid_corpus_id(value))
        except Exception:
            continue
    return valid


def _episode_ref(value) -> str:
    if not isinstance(value, str) or len(value) > _MAX_EPISODE_REF_LENGTH:
        raise ValueError("episode reference is invalid")
    EpisodeReference.parse(value)
    return value


def _optional_episode_ref(value) -> str | None:
    if value is None:
        return None
    try:
        return _episode_ref(value)
    except Exception:
        return None


def _episode_refs(values) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > _MAX_EPISODE_REFS:
        raise ValueError("episode references must be bounded")
    return [_episode_ref(value) for value in values]


def _correlation_token(value) -> str:
    if not isinstance(value, str) or len(value) > _MAX_OPAQUE_IDENTIFIER_LENGTH:
        raise ValueError("opaque identifier is invalid")
    try:
        normalized = normalize_machine_uuid(value)
    except ValueError:
        return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
    if normalized == value:
        return value
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _required_enum(value, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("operational enum is invalid")
    return value


def _nonnegative_int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("operational count is invalid")
    return value


def _duration(value) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
        or not math.isfinite(value)
    ):
        raise ValueError("duration is invalid")
    return value


def _bool(value) -> bool:
    if not isinstance(value, bool):
        raise ValueError("boolean is invalid")
    return value


def _report_failure(exc: BaseException) -> None:
    try:
        sys.stderr.write(f"operational event write failed: {type(exc).__name__}\n")
    except BaseException:
        pass
