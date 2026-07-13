from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from llm_memory.adapters import EpisodeRecord, ScanCursor, SourceMember, get_adapter
from llm_memory.contract import (
    EpisodeBody,
    EpisodeIdentity,
    EpisodeReference,
    FreshnessStanding,
    SourceStanding,
)
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    SOURCE_STATES,
    SUPERSESSIONS,
    activate_generation,
    delete_generation,
    ensure_contract_index,
    write_generation,
)
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment


@dataclass
class WorkBudget:
    max_bytes: int
    now: datetime
    bytes_read: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_bytes, bool)
            or not isinstance(self.max_bytes, int)
            or self.max_bytes < 1
        ):
            raise ValueError("max_bytes must be a positive integer")
        if not isinstance(self.now, datetime):
            raise ValueError("now must be a datetime")

    @property
    def exhausted(self) -> bool:
        return self.bytes_read >= self.max_bytes

    @property
    def remaining(self) -> int:
        return max(1, self.max_bytes - self.bytes_read)

    def charge(self, value: int) -> None:
        self.bytes_read += value


@dataclass(frozen=True)
class ReconcileReport:
    corpus_standing: tuple[dict, ...]
    bytes_read: int
    elapsed_ms: float
    work_exhausted: bool


def extend_chain(previous_hex: str, episode_ref: str) -> str:
    previous = bytes.fromhex(previous_hex) if previous_hex else bytes(32)
    return hashlib.sha256(previous + episode_ref.encode("utf-8")).hexdigest()


def _state_key(corpus_id: str, source_id: str, member_id: str) -> str:
    return hashlib.sha256(f"{corpus_id}/{source_id}/{member_id}".encode()).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _source_states(db, enrollment: SourceEnrollment) -> tuple[dict, ...]:
    return tuple(
        db.aql.execute(
            """
            FOR state IN @@states
                FILTER state.corpus_id == @corpus_id
                FILTER state.source_id == @source_id
                SORT state.member_id
                RETURN UNSET(state, "_id", "_rev")
            """,
            bind_vars={
                "@states": SOURCE_STATES,
                "corpus_id": enrollment.corpus_id,
                "source_id": enrollment.source_id,
            },
        )
    )


def _member_state(db, enrollment: SourceEnrollment, member_id: str) -> dict | None:
    states = [state for state in _source_states(db, enrollment) if state["member_id"] == member_id]
    return states[0] if states else None


def _patch_state(
    db,
    enrollment: SourceEnrollment,
    member_id: str,
    values: dict[str, Any],
) -> dict:
    key = _state_key(enrollment.corpus_id, enrollment.source_id, member_id)
    identity = {
        "_key": key,
        "corpus_id": enrollment.corpus_id,
        "source_id": enrollment.source_id,
        "member_id": member_id,
        "active_generation_id": None,
        "staging_generation_id": None,
    }
    return list(
        db.aql.execute(
            """
            UPSERT { _key: @key }
                INSERT MERGE(@identity, @values)
                UPDATE @values
                IN @@states
                RETURN UNSET(NEW, "_id", "_rev")
            """,
            bind_vars={
                "@states": SOURCE_STATES,
                "key": key,
                "identity": identity,
                "values": values,
            },
        )
    )[0]


def _generation_documents(db, state: dict, generation_id: str | None = None) -> tuple[dict, ...]:
    generation_id = generation_id or state.get("active_generation_id")
    if not generation_id:
        return ()
    return tuple(
        db.aql.execute(
            """
            FOR episode IN @@episodes
                FILTER episode.corpus_id == @corpus_id
                FILTER episode.source_id == @source_id
                FILTER episode.member_id == @member_id
                FILTER episode.generation_id == @generation_id
                SORT episode.source_position.start, episode.episode_ref
                RETURN UNSET(episode, "_id", "_rev")
            """,
            bind_vars={
                "@episodes": CONTRACT_EPISODES,
                "corpus_id": state["corpus_id"],
                "source_id": state["source_id"],
                "member_id": state["member_id"],
                "generation_id": generation_id,
            },
        )
    )


def _episode_from_document(document: dict) -> EpisodeRecord:
    body = EpisodeBody(
        timestamp=document["timestamp"],
        model=document["model"],
        user_message=document["user_message"],
        response=document["response"],
        state=document["state"],
        activity_log=document["activity_log"],
        adapter_fields=document["adapter_fields"],
    )
    return EpisodeRecord(
        identity=EpisodeIdentity(
            EpisodeReference.parse(document["episode_ref"]),
            document["body_digest"],
        ),
        body=body,
        native_event_id=document.get("native_event_id"),
        source_position=document["source_position"],
        state_text=document.get("state_text", ""),
    )


def _stat(member: SourceMember) -> dict | None:
    try:
        stat = member.path.stat()
    except OSError:
        return None
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _cursor(value: dict | None) -> ScanCursor:
    value = value or {}
    return ScanCursor(value.get("byte_offset", 0), value.get("adapter_state", {}))


def _cursor_dict(cursor: ScanCursor) -> dict:
    return {"byte_offset": cursor.byte_offset, "adapter_state": cursor.adapter_state}


def _begin_build(
    db,
    enrollment: SourceEnrollment,
    member: SourceMember,
    state: dict | None,
    reason: str,
) -> dict:
    state = state or {}
    old_staging = state.get("staging_generation_id")
    if old_staging:
        delete_generation(
            db,
            enrollment.corpus_id,
            enrollment.source_id,
            member.member_id,
            old_staging,
        )
    mode = "append" if reason == "append" and state.get("active_generation_id") else "replace"
    adapter_state = state.get("tail_cursor", {}).get("adapter_state", {}) if mode == "append" else {}
    offset = state.get("complete_end", 0) if mode == "append" else 0
    return _patch_state(
        db,
        enrollment,
        member.member_id,
        {
            "build_generation_id": uuid4().hex,
            "build_mode": mode,
            "build_reason": reason,
            "build_cursor": {"byte_offset": offset, "adapter_state": adapter_state},
            "build_seeded": False,
            "freshness": FreshnessStanding.STALE.value if state.get("active_generation_id") else FreshnessStanding.INCOMPLETE.value,
        },
    )


def _record_supersessions(
    db,
    enrollment: SourceEnrollment,
    member: SourceMember,
    old_documents: tuple[dict, ...],
    new_documents: tuple[dict, ...],
    reason: str,
    detected_at: datetime,
) -> None:
    old_by_event = {
        EpisodeReference.parse(document["episode_ref"]).event_token: document["episode_ref"]
        for document in old_documents
    }
    for document in new_documents:
        new_ref = document["episode_ref"]
        event_token = EpisodeReference.parse(new_ref).event_token
        old_ref = old_by_event.get(event_token)
        if not old_ref or old_ref == new_ref:
            continue
        key = hashlib.sha256(f"{old_ref}\0{new_ref}".encode()).hexdigest()
        db.collection(SUPERSESSIONS).insert(
            {
                "_key": key,
                "corpus_id": enrollment.corpus_id,
                "source_id": enrollment.source_id,
                "member_id": member.member_id,
                "event_token": event_token,
                "old_ref": old_ref,
                "new_ref": new_ref,
                "reason": reason,
                "detected_at": _timestamp(detected_at),
            },
            overwrite=True,
        )


def _tail_needed(enrollment: SourceEnrollment, member: SourceMember, state: dict | None) -> tuple[bool, str]:
    if not state or not state.get("active_generation_id"):
        return True, state.get("build_reason", "initial") if state else "initial"
    if state.get("build_generation_id"):
        return True, state.get("build_reason", "initial")
    if (
        state.get("canonicalization_version") != enrollment.canonicalization_version
        or state.get("boundary_version") != enrollment.boundary_version
    ):
        return True, "semantic_version"
    generation = _stat(member)
    if generation and generation["size"] > state.get("complete_end", 0):
        return True, "append"
    return False, ""


def _reconcile_tail(
    db,
    enrollment: SourceEnrollment,
    member: SourceMember,
    budget: WorkBudget,
) -> None:
    adapter = get_adapter(enrollment.adapter)
    state = _member_state(db, enrollment, member.member_id)
    needed, reason = _tail_needed(enrollment, member, state)
    if not needed or budget.exhausted:
        return
    if not state or not state.get("build_generation_id") or state.get("build_reason") != reason:
        state = _begin_build(db, enrollment, member, state, reason)

    generation_id = state["build_generation_id"]
    if state["build_mode"] == "append" and not state.get("build_seeded"):
        active = _generation_documents(db, state)
        write_generation(
            db,
            enrollment,
            member,
            generation_id,
            (_episode_from_document(document) for document in active),
        )
        state = _patch_state(db, enrollment, member.member_id, {"build_seeded": True})

    chunk = adapter.scan_chunk(
        enrollment,
        member,
        _cursor(state.get("build_cursor")),
        budget.remaining,
    )
    budget.charge(chunk.bytes_read)
    if chunk.episodes or not state.get("staging_generation_id"):
        write_generation(db, enrollment, member, generation_id, chunk.episodes)
    generation = _stat(member)
    state = _patch_state(
        db,
        enrollment,
        member.member_id,
        {
            "build_cursor": _cursor_dict(chunk.next_cursor),
            "observed_end": chunk.observed_end,
            "complete_end": chunk.complete_end,
            "source_standing": chunk.source_standing.value,
            "error_position": chunk.error_position,
            "member_generation": generation,
            "freshness": chunk.freshness.value,
            "implementation_version": adapter.implementation_version,
        },
    )
    if chunk.exhausted:
        return

    old_generation = state.get("active_generation_id")
    old_documents = _generation_documents(db, state, old_generation)
    activation_state = {
        "implementation_version": adapter.implementation_version,
        "observed_end": chunk.observed_end,
        "complete_end": chunk.complete_end,
        "member_generation": generation,
        "source_standing": chunk.source_standing.value,
        "freshness": chunk.freshness.value if chunk.freshness is not FreshnessStanding.CURRENT else FreshnessStanding.TAIL_VALIDATED.value,
        "error_position": chunk.error_position,
        "tail_cursor": _cursor_dict(chunk.next_cursor),
        "build_generation_id": None,
        "build_mode": None,
        "build_reason": None,
        "build_cursor": None,
        "build_seeded": None,
        "validated_at": None,
        "integrity_audit": {
            "offset": 0,
            "cursor": {"byte_offset": 0, "adapter_state": {}},
            "chain_digest": "",
            "start_size": 0,
            "start_mtime_ns": 0,
            "start_observed_end": 0,
            "restart_count": 0,
            "bytes_read": 0,
            "elapsed_ms": 0.0,
        },
    }
    activate_generation(db, enrollment, member, generation_id, activation_state)
    new_state = _member_state(db, enrollment, member.member_id)
    new_documents = _generation_documents(db, new_state)
    _record_supersessions(
        db,
        enrollment,
        member,
        old_documents,
        new_documents,
        reason,
        budget.now,
    )
    if old_generation and old_generation != generation_id:
        delete_generation(db, enrollment.corpus_id, enrollment.source_id, member.member_id, old_generation)


def _audit_due(enrollment: SourceEnrollment, member: SourceMember, state: dict | None, now: datetime) -> bool:
    if not state or not state.get("active_generation_id") or state.get("build_generation_id"):
        return False
    if state.get("source_standing") != SourceStanding.AVAILABLE.value:
        return False
    if state.get("complete_end") != state.get("observed_end"):
        return False
    validated = _parse_timestamp(state.get("validated_at"))
    expired = validated is None or (now - validated).total_seconds() > enrollment.full_validation_max_age_seconds
    adapter_changed = state.get("implementation_version") != get_adapter(enrollment.adapter).implementation_version
    generation_changed = _stat(member) != state.get("member_generation")
    return state.get("freshness") != FreshnessStanding.CURRENT.value or expired or adapter_changed or generation_changed


def _restart_audit(state: dict, member_generation: dict | None) -> dict:
    previous = state.get("integrity_audit") or {}
    return {
        "offset": 0,
        "cursor": {"byte_offset": 0, "adapter_state": {}},
        "chain_digest": "",
        "start_size": member_generation["size"] if member_generation else 0,
        "start_mtime_ns": member_generation["mtime_ns"] if member_generation else 0,
        "start_observed_end": member_generation["size"] if member_generation else 0,
        "restart_count": previous.get("restart_count", 0) + (1 if previous.get("offset") else 0),
        "bytes_read": previous.get("bytes_read", 0),
        "elapsed_ms": previous.get("elapsed_ms", 0.0),
    }


def _reconcile_audit(
    db,
    enrollment: SourceEnrollment,
    member: SourceMember,
    budget: WorkBudget,
) -> None:
    state = _member_state(db, enrollment, member.member_id)
    if not _audit_due(enrollment, member, state, budget.now) or budget.exhausted:
        return
    generation = _stat(member)
    audit = state.get("integrity_audit") or {}
    started_generation = {
        "size": audit.get("start_size"),
        "mtime_ns": audit.get("start_mtime_ns"),
    }
    if not audit.get("start_size") and generation and generation["size"] == 0:
        started_generation = generation
    if (
        state.get("freshness") == FreshnessStanding.CURRENT.value
        or not audit.get("start_observed_end")
        or started_generation != generation
    ):
        audit = _restart_audit(state, generation)

    started = time.perf_counter()
    adapter = get_adapter(enrollment.adapter)
    chunk = adapter.scan_chunk(
        enrollment,
        member,
        _cursor(audit.get("cursor")),
        budget.remaining,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    budget.charge(chunk.bytes_read)
    chain = audit.get("chain_digest", "")
    for episode in chunk.episodes:
        chain = extend_chain(chain, episode.identity.episode_ref)

    active_documents = _generation_documents(db, state)
    expected = tuple(
        document["episode_ref"]
        for document in active_documents
        if document["source_position"]["start"] >= audit.get("offset", 0)
        and document["source_position"]["end"] <= chunk.next_cursor.byte_offset
    )
    actual = tuple(episode.identity.episode_ref for episode in chunk.episodes)
    mismatch = expected != actual
    updated_audit = {
        **audit,
        "offset": chunk.next_cursor.byte_offset,
        "cursor": _cursor_dict(chunk.next_cursor),
        "chain_digest": chain,
        "bytes_read": audit.get("bytes_read", 0) + chunk.bytes_read,
        "elapsed_ms": audit.get("elapsed_ms", 0.0) + elapsed_ms,
    }
    if mismatch:
        _patch_state(
            db,
            enrollment,
            member.member_id,
            {
                "integrity_audit": updated_audit,
                "freshness": FreshnessStanding.STALE.value,
                "build_generation_id": None,
                "build_reason": "source_content",
            },
        )
        _begin_build(db, enrollment, member, _member_state(db, enrollment, member.member_id), "source_content")
        return

    if chunk.exhausted:
        _patch_state(
            db,
            enrollment,
            member.member_id,
            {"integrity_audit": updated_audit, "freshness": FreshnessStanding.TAIL_VALIDATED.value},
        )
        return

    if generation != _stat(member) or chunk.observed_end != updated_audit["start_observed_end"]:
        restarted = _restart_audit({"integrity_audit": updated_audit}, _stat(member))
        _patch_state(
            db,
            enrollment,
            member.member_id,
            {"integrity_audit": restarted, "freshness": FreshnessStanding.TAIL_VALIDATED.value},
        )
        return

    _patch_state(
        db,
        enrollment,
        member.member_id,
        {
            "integrity_audit": updated_audit,
            "validated_at": _timestamp(budget.now),
            "member_generation": generation,
            "implementation_version": adapter.implementation_version,
            "source_standing": chunk.source_standing.value,
            "freshness": FreshnessStanding.CURRENT.value,
        },
    )


def _mark_due_audit(
    db,
    enrollment: SourceEnrollment,
    member: SourceMember,
    now: datetime,
) -> None:
    state = _member_state(db, enrollment, member.member_id)
    if (
        state
        and state.get("freshness") == FreshnessStanding.CURRENT.value
        and _audit_due(enrollment, member, state, now)
    ):
        audit = _restart_audit(state, _stat(member))
        audit["restart_count"] = 0
        _patch_state(
            db,
            enrollment,
            member.member_id,
            {
                "freshness": FreshnessStanding.TAIL_VALIDATED.value,
                "integrity_audit": audit,
            },
        )


def _member_standing(state: dict | None, member_id: str, now: datetime, *, missing: bool = False) -> dict:
    state = state or {}
    validated = _parse_timestamp(state.get("validated_at"))
    age = max(0.0, (now - validated).total_seconds()) if validated else None
    audit = state.get(
        "integrity_audit",
        {"offset": 0, "chain_digest": "", "restart_count": 0, "bytes_read": 0, "elapsed_ms": 0.0},
    )
    if state.get("active_generation_id"):
        index_standing = "available"
    elif state.get("staging_generation_id") or state.get("build_generation_id"):
        index_standing = "rebuilding"
    else:
        index_standing = "unavailable"
    return {
        "member_id": member_id,
        "source_standing": SourceStanding.MISSING.value if missing else state.get("source_standing", SourceStanding.UNKNOWN.value),
        "index_standing": index_standing,
        "freshness": FreshnessStanding.UNAVAILABLE.value if missing else state.get("freshness", FreshnessStanding.UNKNOWN.value),
        "indexed_through": {"kind": "byte_offset", "value": state.get("complete_end", 0)},
        "observed_source_end": {"kind": "byte_offset", "value": state.get("observed_end", 0)},
        "error_position": state.get("error_position"),
        "integrity": {
            "basis": "full_digest",
            "validated_at": state.get("validated_at"),
            "validation_age_seconds": age,
            "audit_offset": audit.get("offset", 0),
            "chain_digest": audit.get("chain_digest", ""),
            "bytes_read": audit.get("bytes_read", 0),
            "elapsed_ms": audit.get("elapsed_ms", 0.0),
            "restart_count": audit.get("restart_count", 0),
        },
    }


def _source_standing(
    db,
    enrollment: SourceEnrollment,
    live_members: tuple[SourceMember, ...],
    now: datetime,
) -> dict:
    states = {state["member_id"]: state for state in _source_states(db, enrollment)}
    live_ids = {member.member_id for member in live_members}
    member_reports = [
        _member_standing(states.get(member.member_id), member.member_id, now)
        for member in live_members
    ]
    member_reports.extend(
        _member_standing(state, member_id, now, missing=True)
        for member_id, state in states.items()
        if member_id not in live_ids
    )
    member_reports.sort(key=lambda report: report["member_id"])
    adapter = get_adapter(enrollment.adapter)
    try:
        enrollment.locator.stat()
    except FileNotFoundError:
        source_set_standing = SourceStanding.MISSING.value
    except OSError:
        source_set_standing = SourceStanding.UNAVAILABLE.value
    else:
        source_set_standing = SourceStanding.AVAILABLE.value
    return {
        "source_id": enrollment.source_id,
        "adapter": enrollment.adapter,
        "implementation_version": adapter.implementation_version,
        "canonicalization_version": enrollment.canonicalization_version,
        "boundary_version": enrollment.boundary_version,
        "source_set_standing": source_set_standing,
        "members": tuple(member_reports),
    }


def reconcile_registry(
    db,
    registry: EnrollmentRegistry,
    budget: WorkBudget,
) -> ReconcileReport:
    started = time.perf_counter()
    initial_bytes = budget.bytes_read
    ensure_contract_index(db)
    sources = tuple(sorted((source for source in registry.sources if source.enabled), key=lambda source: (source.corpus_id, source.source_id)))
    discovered = {
        (source.corpus_id, source.source_id): tuple(sorted(get_adapter(source.adapter).members(source), key=lambda member: member.member_id))
        for source in sources
    }

    for source in sources:
        for member in discovered[(source.corpus_id, source.source_id)]:
            _reconcile_tail(db, source, member, budget)

    audit_candidates: list[tuple[datetime, SourceEnrollment, SourceMember]] = []
    for source in sources:
        for member in discovered[(source.corpus_id, source.source_id)]:
            _mark_due_audit(db, source, member, budget.now)
            state = _member_state(db, source, member.member_id)
            if _audit_due(source, member, state, budget.now):
                audit_candidates.append((_parse_timestamp(state.get("validated_at")) or datetime.min.replace(tzinfo=UTC), source, member))
    for _, source, member in sorted(audit_candidates, key=lambda item: (item[0], item[1].corpus_id, item[1].source_id, item[2].member_id)):
        _reconcile_audit(db, source, member, budget)

    corpus_reports = []
    for corpus_id in sorted({source.corpus_id for source in sources}):
        source_reports = tuple(
            _source_standing(db, source, discovered[(source.corpus_id, source.source_id)], budget.now)
            for source in sources
            if source.corpus_id == corpus_id
        )
        corpus_reports.append({"corpus_id": corpus_id, "sources": source_reports})
    elapsed_ms = (time.perf_counter() - started) * 1000
    return ReconcileReport(
        corpus_standing=tuple(corpus_reports),
        bytes_read=budget.bytes_read - initial_bytes,
        elapsed_ms=elapsed_ms,
        work_exhausted=budget.exhausted,
    )


def reconcile_source(
    db,
    enrollment: SourceEnrollment,
    budget: WorkBudget,
) -> tuple[dict, ...]:
    report = reconcile_registry(db, EnrollmentRegistry((enrollment,)), budget)
    return report.corpus_standing[0]["sources"][0]["members"]


def reconcile_member(
    db,
    enrollment: SourceEnrollment,
    member: SourceMember,
    budget: WorkBudget,
) -> dict:
    ensure_contract_index(db)
    _reconcile_tail(db, enrollment, member, budget)
    _reconcile_audit(db, enrollment, member, budget)
    return _member_standing(_member_state(db, enrollment, member.member_id), member.member_id, budget.now)
