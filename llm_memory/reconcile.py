from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from arango.exceptions import AQLQueryExecuteError

from llm_memory.adapters import (
    MemberChunk,
    ScanCursor,
    SourceMember,
    get_adapter,
)
from llm_memory.contract import (
    EpisodeReference,
    FreshnessStanding,
    SourceStanding,
)
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    GenerationDocumentConflict,
    GenerationStateConflict,
    SOURCE_STATES,
    SUPERSESSIONS,
    activate_generation,
    delete_generation,
    ensure_contract_index,
    generation_count,
    seed_generation,
    write_generation,
)
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment


class _StateConflict(RuntimeError):
    pass


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
                RETURN UNSET(state, "_id")
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
    *,
    expected_state: dict | None | object = ...,
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
    bind_vars = {
        "@states": SOURCE_STATES,
        "key": key,
        "identity": identity,
        "values": values,
    }
    if expected_state is ...:
        query = """
        UPSERT { _key: @key }
            INSERT MERGE(@identity, @values)
            UPDATE @values
            IN @@states
            RETURN UNSET(NEW, "_id")
        """
    elif expected_state is None:
        query = """
        LET existing = DOCUMENT(@@states, @key)
        FILTER existing == null
        INSERT MERGE(@identity, @values) IN @@states
        RETURN UNSET(NEW, "_id")
        """
    else:
        query = """
        FOR current IN @@states
            FILTER current._key == @key
            FILTER current._rev == @expected_revision
            FILTER current.active_generation_id == @expected_active_generation_id
            FILTER current.build_generation_id == @expected_build_generation_id
            FILTER current.build_cursor == @expected_build_cursor
            UPDATE current WITH @values IN @@states
            RETURN UNSET(NEW, "_id")
        """
        bind_vars.update(
            {
                "expected_revision": expected_state.get("_rev"),
                "expected_active_generation_id": expected_state.get(
                    "active_generation_id"
                ),
                "expected_build_generation_id": expected_state.get(
                    "build_generation_id"
                ),
                "expected_build_cursor": expected_state.get("build_cursor"),
            }
        )
        bind_vars.pop("identity")
    try:
        updated = list(db.aql.execute(query, bind_vars=bind_vars))
    except AQLQueryExecuteError as exc:
        if exc.error_code not in {1200, 1210}:
            raise
        raise _StateConflict(
            f"source state raced for {enrollment.source_id}/{member_id}"
        ) from exc
    if not updated:
        raise _StateConflict(
            f"source state changed for {enrollment.source_id}/{member_id}"
        )
    return updated[0]


def _generation_documents(
    db,
    state: dict,
    generation_id: str | None = None,
    *,
    start: int | None = None,
    end: int | None = None,
) -> tuple[dict, ...]:
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
                FILTER @start == null OR episode.source_position.start >= @start
                FILTER @end == null OR episode.source_position.end <= @end
                SORT episode.source_position.start, episode.episode_ref
                RETURN UNSET(episode, "_id", "_rev")
            """,
            bind_vars={
                "@episodes": CONTRACT_EPISODES,
                "corpus_id": state["corpus_id"],
                "source_id": state["source_id"],
                "member_id": state["member_id"],
                "generation_id": generation_id,
                "start": start,
                "end": end,
            },
        )
    )


def _active_generation_backed(db, state: dict | None) -> bool:
    if not state or not state.get("active_generation_id"):
        return False
    expected = state.get("episode_count")
    return expected is not None and generation_count(db, state) == expected


def _same_transition(current: dict | None, expected: dict) -> bool:
    return bool(
        current
        and current.get("_rev") == expected.get("_rev")
        and current.get("active_generation_id")
        == expected.get("active_generation_id")
        and current.get("build_generation_id")
        == expected.get("build_generation_id")
        and current.get("build_cursor") == expected.get("build_cursor")
    )


def _stat(member: SourceMember) -> dict | None:
    try:
        stat = member.path.stat()
    except OSError:
        return None
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def _audit_snapshot_stable(
    audit: dict,
    generation: dict | None,
    *,
    compatibility_audit: bool,
) -> bool:
    if generation is None:
        return False
    started = {
        "size": audit.get("start_size"),
        "mtime_ns": audit.get("start_mtime_ns"),
        "device": audit.get("start_device"),
        "inode": audit.get("start_inode"),
    }
    if not compatibility_audit:
        return started == generation

    target_end = audit.get("target_end", 0)
    if (
        started["device"] is None
        or started["inode"] is None
        or generation["device"] != started["device"]
        or generation["inode"] != started["inode"]
        or generation["size"] < target_end
        or generation["size"] < started["size"]
    ):
        return False
    if generation["size"] == started["size"]:
        return generation["mtime_ns"] == started["mtime_ns"]

    # Same-inode monotonic growth is the append-only signal. An in-place prefix
    # rewrite followed by an append is outside the external-writer contract.
    return started["size"] >= target_end


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
    expected_state = state
    state = state or {}
    old_staging = state.get("staging_generation_id")
    mode = "append" if reason == "append" and state.get("active_generation_id") else "replace"
    adapter_state = state.get("tail_cursor", {}).get("adapter_state", {}) if mode == "append" else {}
    offset = state.get("complete_end", 0) if mode == "append" else 0
    updated = _patch_state(
        db,
        enrollment,
        member.member_id,
        {
            "build_generation_id": uuid4().hex,
            "build_mode": mode,
            "build_reason": reason,
            "build_canonicalization_version": enrollment.canonicalization_version,
            "build_boundary_version": enrollment.boundary_version,
            "build_cursor": {"byte_offset": offset, "adapter_state": adapter_state},
            "build_seeded": False,
            "staging_generation_id": None,
            "staging_episode_count": None,
            "staging_canonicalization_version": None,
            "staging_boundary_version": None,
            "active_generation_integrity": (
                "invalid"
                if reason == "pending_source_change"
                else state.get("active_generation_integrity")
            ),
            "freshness": FreshnessStanding.STALE.value if state.get("active_generation_id") else FreshnessStanding.INCOMPLETE.value,
        },
        expected_state=expected_state,
    )
    if old_staging:
        delete_generation(
            db,
            enrollment.corpus_id,
            enrollment.source_id,
            member.member_id,
            old_staging,
        )
    return updated


def _record_supersessions(
    db,
    enrollment: SourceEnrollment,
    member_id: str,
    old_documents: tuple[dict, ...],
    new_documents: tuple[dict, ...],
    reason: str,
    detected_at: datetime,
) -> None:
    old_by_event = {}
    for document in old_documents:
        reference = EpisodeReference.parse(document["episode_ref"])
        old_by_event[(reference.native_session_id, reference.event_token)] = document[
            "episode_ref"
        ]
    for document in new_documents:
        new_ref = document["episode_ref"]
        reference = EpisodeReference.parse(new_ref)
        event_token = reference.event_token
        old_ref = old_by_event.get((reference.native_session_id, event_token))
        if not old_ref or old_ref == new_ref:
            continue
        key = hashlib.sha256(f"{old_ref}\0{new_ref}".encode()).hexdigest()
        db.collection(SUPERSESSIONS).insert(
            {
                "_key": key,
                "corpus_id": enrollment.corpus_id,
                "source_id": enrollment.source_id,
                "member_id": member_id,
                "event_token": event_token,
                "old_ref": old_ref,
                "new_ref": new_ref,
                "reason": reason,
                "detected_at": _timestamp(detected_at),
            },
            overwrite=True,
        )


def _finalize_supersessions(
    db,
    enrollment: SourceEnrollment,
    state: dict,
) -> None:
    pending = state.get("supersession_finalization")
    if not pending:
        return
    old_generation = pending["old_generation_id"]
    new_generation = pending["new_generation_id"]
    if state.get("active_generation_id") != new_generation:
        return
    if pending["reason"] != "append":
        old_documents = _generation_documents(db, state, old_generation)
        new_documents = _generation_documents(db, state, new_generation)
        _record_supersessions(
            db,
            enrollment,
            state["member_id"],
            old_documents,
            new_documents,
            pending["reason"],
            _parse_timestamp(pending["detected_at"]),
        )
    delete_generation(
        db,
        enrollment.corpus_id,
        enrollment.source_id,
        state["member_id"],
        old_generation,
    )
    try:
        _patch_state(
            db,
            enrollment,
            state["member_id"],
            {"supersession_finalization": None},
            expected_state=state,
        )
    except _StateConflict:
        pass


def _tail_needed(enrollment: SourceEnrollment, member: SourceMember, state: dict | None) -> tuple[bool, str]:
    if state and state.get("build_generation_id"):
        if (
            state.get("build_canonicalization_version")
            != enrollment.canonicalization_version
            or state.get("build_boundary_version") != enrollment.boundary_version
        ):
            return True, "semantic_version"
        return True, state.get("build_reason", "initial")
    if not state or not state.get("active_generation_id"):
        return True, "initial"
    if (
        state.get("canonicalization_version") != enrollment.canonicalization_version
        or state.get("boundary_version") != enrollment.boundary_version
    ):
        return True, "semantic_version"
    generation = _stat(member)
    if state.get("complete_end", 0) < state.get("observed_end", 0):
        published = state.get("member_generation")
        appendable = bool(
            generation
            and published
            and generation.get("device") == published.get("device")
            and generation.get("inode") == published.get("inode")
            and generation["size"] >= published.get("size", 0)
            and (
                generation["size"] > published.get("size", 0)
                or generation.get("mtime_ns") == published.get("mtime_ns")
            )
        )
        return True, "append" if appendable else "pending_source_change"
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
    if (
        state
        and state.get("active_generation_id")
        and state.get("implementation_version") != adapter.implementation_version
        and state.get("canonicalization_version")
        == enrollment.canonicalization_version
        and state.get("boundary_version") == enrollment.boundary_version
    ):
        return
    if state and state.get("active_generation_id") and not _active_generation_backed(db, state):
        needed, reason = True, "derived_loss"
    else:
        needed, reason = _tail_needed(enrollment, member, state)
    if not needed or budget.exhausted:
        return
    try:
        if (
            not state
            or not state.get("build_generation_id")
            or state.get("build_reason") != reason
            or state.get("build_canonicalization_version")
            != enrollment.canonicalization_version
            or state.get("build_boundary_version") != enrollment.boundary_version
        ):
            state = _begin_build(db, enrollment, member, state, reason)
    except _StateConflict:
        return

    generation_id = state["build_generation_id"]
    if state["build_mode"] == "append" and not state.get("build_seeded"):
        transition = state
        try:
            seed_generation(
                db,
                enrollment,
                member,
                state["active_generation_id"],
                generation_id,
                expected_state=transition,
            )
        except (GenerationDocumentConflict, GenerationStateConflict):
            return
        state = _member_state(db, enrollment, member.member_id)
        if not state or any(
            state.get(field) != transition.get(field)
            for field in (
                "active_generation_id",
                "build_generation_id",
                "build_cursor",
            )
        ):
            return
        try:
            state = _patch_state(
                db,
                enrollment,
                member.member_id,
                {"build_seeded": True},
                expected_state=state,
            )
        except _StateConflict:
            return

    transition = state
    chunk = adapter.scan_chunk(
        enrollment,
        member,
        _cursor(state.get("build_cursor")),
        budget.remaining,
    )
    budget.charge(chunk.bytes_read)
    if not _same_transition(
        _member_state(db, enrollment, member.member_id), transition
    ):
        return
    if chunk.episodes or state.get("staging_generation_id") != generation_id:
        try:
            write_generation(
                db,
                enrollment,
                member,
                generation_id,
                chunk.episodes,
                expected_state=transition,
            )
        except GenerationDocumentConflict as conflict:
            current = _member_state(db, enrollment, member.member_id)
            if _same_transition(current, transition):
                try:
                    _patch_state(
                        db,
                        enrollment,
                        member.member_id,
                        {
                            "observed_end": chunk.observed_end,
                            "source_standing": SourceStanding.MALFORMED.value,
                            "freshness": FreshnessStanding.UNKNOWN.value,
                            "error_position": conflict.error_position,
                            "member_generation": _stat(member),
                        },
                        expected_state=current,
                    )
                except _StateConflict:
                    pass
            return
        except GenerationStateConflict:
            return
    state = _member_state(db, enrollment, member.member_id)
    if not state or any(
        state.get(field) != transition.get(field)
        for field in (
            "active_generation_id",
            "build_generation_id",
            "build_cursor",
        )
    ):
        return
    generation = _stat(member)
    try:
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
                "freshness": (
                    FreshnessStanding.STALE.value
                    if state.get("active_generation_integrity") == "invalid"
                    and chunk.source_standing is SourceStanding.AVAILABLE
                    else FreshnessStanding.INCOMPLETE.value
                    if chunk.freshness is FreshnessStanding.INCOMPLETE
                    or chunk.exhausted
                    else FreshnessStanding.UNAVAILABLE.value
                    if chunk.source_standing
                    in {SourceStanding.MISSING, SourceStanding.UNAVAILABLE}
                    else FreshnessStanding.UNKNOWN.value
                    if chunk.source_standing is not SourceStanding.AVAILABLE
                    or chunk.error_position is not None
                    else FreshnessStanding.TAIL_VALIDATED.value
                ),
                "implementation_version": adapter.implementation_version,
            },
            expected_state=state,
        )
    except _StateConflict:
        return
    if chunk.exhausted:
        return
    source_failed = (
        chunk.source_standing is not SourceStanding.AVAILABLE
        or chunk.error_position is not None
        or chunk.freshness is FreshnessStanding.INCOMPLETE
        or chunk.complete_end < chunk.observed_end
    )
    if source_failed and (
        state.get("active_generation_id")
        or not state.get("staging_episode_count")
    ):
        return

    old_generation = state.get("active_generation_id")
    pending_finalization = None
    if old_generation and old_generation != generation_id:
        pending_finalization = {
            "old_generation_id": old_generation,
            "new_generation_id": generation_id,
            "reason": reason,
            "detected_at": _timestamp(budget.now),
        }
        try:
            state = _patch_state(
                db,
                enrollment,
                member.member_id,
                {"supersession_finalization": pending_finalization},
                expected_state=state,
            )
        except _StateConflict:
            return
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
        "build_canonicalization_version": None,
        "build_boundary_version": None,
        "build_cursor": None,
        "build_seeded": None,
        "implementation_compatibility": None,
        "incompatible_implementation_version": None,
        "active_generation_integrity": None,
        "validated_at": None,
        "supersession_finalization": pending_finalization,
        "integrity_audit": {
            "offset": 0,
            "cursor": {"byte_offset": 0, "adapter_state": {}},
            "chain_digest": "",
            "start_size": 0,
            "start_mtime_ns": 0,
            "start_device": None,
            "start_inode": None,
            "start_observed_end": 0,
            "target_end": chunk.complete_end,
            "restart_count": 0,
            "episode_count": 0,
            "bytes_read": 0,
            "elapsed_ms": 0.0,
            "trusted_chain_digest": None,
            "trusted_episode_count": None,
        },
    }
    try:
        activate_generation(
            db,
            enrollment,
            member,
            generation_id,
            activation_state,
            expected_state=state,
        )
    except ValueError:
        return
    new_state = _member_state(db, enrollment, member.member_id)
    _finalize_supersessions(db, enrollment, new_state)


def _audit_due(enrollment: SourceEnrollment, member: SourceMember, state: dict | None, now: datetime) -> bool:
    if not state or not state.get("active_generation_id") or state.get("build_generation_id"):
        return False
    if state.get("complete_end") != state.get("observed_end"):
        return False
    validated = _parse_timestamp(state.get("validated_at"))
    expired = validated is None or (now - validated).total_seconds() > enrollment.full_validation_max_age_seconds
    adapter_version = get_adapter(enrollment.adapter).implementation_version
    if (
        state.get("implementation_compatibility") in {"incompatible", "unverified"}
        and state.get("incompatible_implementation_version") == adapter_version
    ):
        return False
    adapter_changed = state.get("implementation_version") != adapter_version
    generation_changed = _stat(member) != state.get("member_generation")
    return state.get("freshness") != FreshnessStanding.CURRENT.value or expired or adapter_changed or generation_changed


def _restart_audit(state: dict, member_generation: dict | None) -> dict:
    previous = state.get("integrity_audit") or {}
    trusted_chain = previous.get("trusted_chain_digest")
    trusted_count = previous.get("trusted_episode_count")
    if trusted_chain is None and state.get("validated_at"):
        trusted_chain = previous.get("chain_digest")
        trusted_count = previous.get("episode_count")
    return {
        "offset": 0,
        "cursor": {"byte_offset": 0, "adapter_state": {}},
        "chain_digest": "",
        "start_size": member_generation["size"] if member_generation else 0,
        "start_mtime_ns": member_generation["mtime_ns"] if member_generation else 0,
        "start_device": member_generation["device"] if member_generation else None,
        "start_inode": member_generation["inode"] if member_generation else None,
        "start_observed_end": member_generation["size"] if member_generation else 0,
        "target_end": previous.get("target_end", state.get("complete_end", 0)),
        "restart_count": previous.get("restart_count", 0) + (1 if previous.get("offset") else 0),
        "episode_count": 0,
        "bytes_read": previous.get("bytes_read", 0),
        "elapsed_ms": previous.get("elapsed_ms", 0.0),
        "trusted_chain_digest": trusted_chain,
        "trusted_episode_count": trusted_count,
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
    adapter = get_adapter(enrollment.adapter)
    implementation_changed = (
        state.get("implementation_version") != adapter.implementation_version
    )
    generation = _stat(member)
    audit = state.get("integrity_audit") or {}
    if (
        state.get("freshness") == FreshnessStanding.CURRENT.value
        or not audit.get("start_observed_end")
        or not _audit_snapshot_stable(
            audit,
            generation,
            compatibility_audit=implementation_changed,
        )
    ):
        audit = _restart_audit(state, generation)

    started = time.perf_counter()
    audit_cursor = _cursor(audit.get("cursor"))
    target_end = audit.get("target_end", state.get("complete_end", 0))
    if audit_cursor.byte_offset >= target_end and generation is not None:
        chunk = MemberChunk(
            member=member,
            episodes=(),
            next_cursor=audit_cursor,
            observed_end=generation["size"],
            complete_end=target_end,
            source_standing=SourceStanding.AVAILABLE,
            freshness=FreshnessStanding.CURRENT,
            bytes_read=0,
            exhausted=False,
        )
    else:
        chunk = adapter.scan_chunk(
            enrollment,
            member,
            audit_cursor,
            min(budget.remaining, max(1, target_end - audit_cursor.byte_offset)),
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    budget.charge(chunk.bytes_read)
    chain = audit.get("chain_digest", "")
    for episode in chunk.episodes:
        chain = extend_chain(chain, episode.identity.episode_ref)

    stored_episode_count = generation_count(db, state)
    active_generation_backed = (
        state.get("episode_count") is not None
        and stored_episode_count == state.get("episode_count")
    )
    expected_documents = _generation_documents(
        db,
        state,
        start=audit.get("offset", 0),
        end=chunk.next_cursor.byte_offset,
    )
    expected = tuple(
        (
            document["episode_ref"],
            document["source_position"]["start"],
            document["source_position"]["end"],
        )
        for document in expected_documents
    )
    actual = tuple(
        (
            episode.identity.episode_ref,
            episode.source_position["start"],
            episode.source_position["end"],
        )
        for episode in chunk.episodes
    )
    mismatch = (
        active_generation_backed and expected != actual
    ) or chunk.observed_end < target_end
    updated_audit = {
        **audit,
        "offset": chunk.next_cursor.byte_offset,
        "cursor": _cursor_dict(chunk.next_cursor),
        "chain_digest": chain,
        "episode_count": audit.get("episode_count", 0) + len(chunk.episodes),
        "bytes_read": audit.get("bytes_read", 0) + chunk.bytes_read,
        "elapsed_ms": audit.get("elapsed_ms", 0.0) + elapsed_ms,
    }
    audit_complete = chunk.next_cursor.byte_offset >= target_end
    if (
        chunk.source_standing is not SourceStanding.AVAILABLE
        or chunk.error_position is not None
    ):
        freshness = (
            FreshnessStanding.UNAVAILABLE.value
            if chunk.source_standing
            in {SourceStanding.MISSING, SourceStanding.UNAVAILABLE}
            else FreshnessStanding.UNKNOWN.value
        )
        try:
            _patch_state(
                db,
                enrollment,
                member.member_id,
                {
                    "integrity_audit": updated_audit,
                    "source_standing": chunk.source_standing.value,
                    "freshness": freshness,
                    "error_position": chunk.error_position,
                },
                expected_state=state,
            )
        except _StateConflict:
            pass
        return
    if (
        chunk.freshness is FreshnessStanding.INCOMPLETE
        and not chunk.exhausted
        and not audit_complete
        and not mismatch
    ):
        try:
            _patch_state(
                db,
                enrollment,
                member.member_id,
                {
                    "integrity_audit": updated_audit,
                    "source_standing": chunk.source_standing.value,
                    "freshness": FreshnessStanding.INCOMPLETE.value,
                },
                expected_state=state,
            )
        except _StateConflict:
            pass
        return
    if mismatch:
        if implementation_changed:
            try:
                _patch_state(
                    db,
                    enrollment,
                    member.member_id,
                    {
                        "integrity_audit": updated_audit,
                        "implementation_compatibility": "incompatible",
                        "incompatible_implementation_version": (
                            adapter.implementation_version
                        ),
                        "freshness": FreshnessStanding.UNKNOWN.value,
                    },
                    expected_state=state,
                )
            except _StateConflict:
                pass
            return
        try:
            updated = _patch_state(
                db,
                enrollment,
                member.member_id,
                {
                    "integrity_audit": updated_audit,
                    "freshness": FreshnessStanding.STALE.value,
                    "build_generation_id": None,
                    "build_reason": "source_content",
                },
                expected_state=state,
            )
            _begin_build(db, enrollment, member, updated, "source_content")
        except _StateConflict:
            pass
        return

    if not audit_complete:
        try:
            _patch_state(
                db,
                enrollment,
                member.member_id,
                {
                    "integrity_audit": updated_audit,
                    "freshness": FreshnessStanding.TAIL_VALIDATED.value,
                    "implementation_compatibility": (
                        "pending" if implementation_changed else state.get(
                            "implementation_compatibility"
                        )
                    ),
                    "incompatible_implementation_version": (
                        adapter.implementation_version
                        if implementation_changed
                        else state.get("incompatible_implementation_version")
                    ),
                },
                expected_state=state,
            )
        except _StateConflict:
            pass
        return

    finished_generation = _stat(member)
    if not _audit_snapshot_stable(
        audit,
        finished_generation,
        compatibility_audit=implementation_changed,
    ) or (
        not implementation_changed
        and chunk.observed_end != updated_audit["start_observed_end"]
    ):
        restarted = _restart_audit(
            {"integrity_audit": updated_audit}, finished_generation
        )
        try:
            _patch_state(
                db,
                enrollment,
                member.member_id,
                {"integrity_audit": restarted, "freshness": FreshnessStanding.TAIL_VALIDATED.value},
                expected_state=state,
            )
        except _StateConflict:
            pass
        return

    if active_generation_backed:
        active_documents = _generation_documents(db, state)
        expected_chain = ""
        for document in active_documents:
            expected_chain = extend_chain(expected_chain, document["episode_ref"])
        expected_count = len(active_documents)
    else:
        expected_chain = audit.get("trusted_chain_digest")
        expected_count = audit.get("trusted_episode_count")
    if expected_chain is None or expected_count is None:
        try:
            _patch_state(
                db,
                enrollment,
                member.member_id,
                {
                    "integrity_audit": updated_audit,
                    "implementation_compatibility": (
                        "unverified" if implementation_changed else state.get(
                            "implementation_compatibility"
                        )
                    ),
                    "incompatible_implementation_version": (
                        adapter.implementation_version
                        if implementation_changed
                        else state.get("incompatible_implementation_version")
                    ),
                    "freshness": FreshnessStanding.UNKNOWN.value,
                },
                expected_state=state,
            )
        except _StateConflict:
            pass
        return
    if (
        updated_audit["episode_count"] != expected_count
        or updated_audit["chain_digest"] != expected_chain
    ):
        if implementation_changed:
            try:
                _patch_state(
                    db,
                    enrollment,
                    member.member_id,
                    {
                        "integrity_audit": updated_audit,
                        "implementation_compatibility": "incompatible",
                        "incompatible_implementation_version": (
                            adapter.implementation_version
                        ),
                        "freshness": FreshnessStanding.UNKNOWN.value,
                    },
                    expected_state=state,
                )
            except _StateConflict:
                pass
            return
        try:
            updated = _patch_state(
                db,
                enrollment,
                member.member_id,
                {
                    "integrity_audit": updated_audit,
                    "freshness": FreshnessStanding.STALE.value,
                    "build_generation_id": None,
                    "build_reason": "source_content",
                },
                expected_state=state,
            )
            _begin_build(db, enrollment, member, updated, "source_content")
        except _StateConflict:
            pass
        return

    completed_audit = {
        **updated_audit,
        "trusted_chain_digest": updated_audit["chain_digest"],
        "trusted_episode_count": updated_audit["episode_count"],
    }
    published_generation = (
        finished_generation if implementation_changed else generation
    )
    published_observed_end = (
        published_generation["size"]
        if implementation_changed
        else chunk.observed_end
    )
    source_has_tail = published_observed_end > target_end
    try:
        _patch_state(
            db,
            enrollment,
            member.member_id,
            {
                "integrity_audit": completed_audit,
                "validated_at": _timestamp(budget.now),
                "member_generation": published_generation,
                "implementation_version": adapter.implementation_version,
                "implementation_compatibility": "compatible",
                "incompatible_implementation_version": None,
                "source_standing": chunk.source_standing.value,
                "observed_end": published_observed_end,
                "freshness": (
                    FreshnessStanding.INCOMPLETE.value
                    if source_has_tail
                    else FreshnessStanding.CURRENT.value
                ),
            },
            expected_state=state,
        )
    except _StateConflict:
        pass


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
        adapter_version = get_adapter(enrollment.adapter).implementation_version
        implementation_changed = state.get("implementation_version") != adapter_version
        audit = _restart_audit(state, _stat(member))
        audit["restart_count"] = 0
        generation = _stat(member)
        source_shrunk = bool(
            generation is not None
            and generation["size"] < state.get("complete_end", 0)
        )
        try:
            _patch_state(
                db,
                enrollment,
                member.member_id,
                {
                    "freshness": (
                        FreshnessStanding.STALE.value
                        if source_shrunk
                        else FreshnessStanding.TAIL_VALIDATED.value
                    ),
                    "integrity_audit": audit,
                    "implementation_compatibility": (
                        "pending" if implementation_changed else state.get(
                            "implementation_compatibility"
                        )
                    ),
                    "incompatible_implementation_version": (
                        adapter_version
                        if implementation_changed
                        else state.get("incompatible_implementation_version")
                    ),
                },
                expected_state=state,
            )
        except _StateConflict:
            pass


def _member_standing(
    db,
    state: dict | None,
    member_id: str,
    now: datetime,
    *,
    missing: bool = False,
) -> dict:
    state = state or {}
    validated = _parse_timestamp(state.get("validated_at"))
    age = max(0.0, (now - validated).total_seconds()) if validated else None
    audit = state.get(
        "integrity_audit",
        {"offset": 0, "chain_digest": "", "restart_count": 0, "bytes_read": 0, "elapsed_ms": 0.0},
    )
    source_standing = (
        SourceStanding.MISSING.value
        if missing
        else state.get("source_standing", SourceStanding.UNKNOWN.value)
    )
    compatibility = state.get("implementation_compatibility")
    incompatible = compatibility not in {None, "compatible"}
    active_integrity_invalid = state.get("active_generation_integrity") == "invalid"
    if (
        state.get("active_generation_id")
        and _active_generation_backed(db, state)
        and not incompatible
        and not active_integrity_invalid
    ):
        index_standing = "available"
    elif (
        source_standing == SourceStanding.AVAILABLE.value
        and (state.get("staging_generation_id") or state.get("build_generation_id"))
    ):
        index_standing = "rebuilding"
    else:
        index_standing = "unavailable"
    return {
        "member_id": member_id,
        "episode_count": state.get("episode_count", 0),
        "source_standing": source_standing,
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
        _member_standing(db, states.get(member.member_id), member.member_id, now)
        for member in live_members
    ]
    member_reports.extend(
        _member_standing(db, state, member_id, now, missing=True)
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
        for state in _source_states(db, source):
            _finalize_supersessions(db, source, state)

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
        before_audit = _member_state(db, source, member.member_id)
        _reconcile_audit(db, source, member, budget)
        after_audit = _member_state(db, source, member.member_id)
        adapter_version = get_adapter(source.adapter).implementation_version
        if (
            before_audit
            and after_audit
            and before_audit.get("implementation_version") != adapter_version
            and after_audit.get("implementation_version") == adapter_version
            and after_audit.get("implementation_compatibility") == "compatible"
        ):
            _reconcile_tail(db, source, member, budget)
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
    state = _member_state(db, enrollment, member.member_id)
    if state:
        _finalize_supersessions(db, enrollment, state)
    _reconcile_tail(db, enrollment, member, budget)
    _mark_due_audit(db, enrollment, member, budget.now)
    _reconcile_audit(db, enrollment, member, budget)
    return _member_standing(
        db,
        _member_state(db, enrollment, member.member_id),
        member.member_id,
        budget.now,
    )
