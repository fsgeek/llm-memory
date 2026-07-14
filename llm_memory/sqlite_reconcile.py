from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from uuid import uuid4

from llm_memory.adapter_versions import supports_semantic_versions
from llm_memory.adapters import ScanCursor, SourceMember, get_adapter
from llm_memory.contract import EpisodeReference, FreshnessStanding, SourceStanding
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.provider import ProviderUnavailable
from llm_memory.reconcile import ReconcileReport, WorkBudget, extend_chain
from llm_memory.sqlite_store import (
    SQLiteDocumentConflict,
    SQLiteStateConflict,
    SQLiteStore,
)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _cursor(value: dict | None) -> ScanCursor:
    value = value or {}
    return ScanCursor(value.get("byte_offset", 0), value.get("adapter_state", {}))


def _cursor_dict(cursor: ScanCursor) -> dict:
    return {"byte_offset": cursor.byte_offset, "adapter_state": cursor.adapter_state}


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


def _begin_build(
    store: SQLiteStore,
    enrollment: SourceEnrollment,
    member: SourceMember,
    state: dict | None,
    reason: str,
) -> dict:
    previous = state or {}
    old_staging = previous.get("staging_generation_id")
    mode = "append" if reason == "append" else "replace"
    generation_id = uuid4().hex
    source_snapshot = _stat(member)
    updated = store.compare_and_swap_state(
        enrollment,
        member.member_id,
        state,
        {
            "staging_generation_id": generation_id,
            "staging_episode_count": 0,
            "staging_canonicalization_version": (
                enrollment.canonicalization_version
            ),
            "staging_boundary_version": enrollment.boundary_version,
            "build_generation_id": generation_id,
            "build_mode": mode,
            "build_reason": reason,
            "build_cursor": (
                previous.get("tail_cursor", {"byte_offset": 0, "adapter_state": {}})
                if mode == "append"
                else {"byte_offset": 0, "adapter_state": {}}
            ),
            "build_chain_digest": (
                previous.get("integrity_audit", {}).get(
                    "trusted_chain_digest", ""
                )
                if mode == "append"
                else ""
            ),
            "build_seeded": False,
            "build_canonicalization_version": enrollment.canonicalization_version,
            "build_boundary_version": enrollment.boundary_version,
            "build_source_snapshot": source_snapshot,
            "build_observed_end": (
                previous.get("observed_end", 0) if mode == "append" else 0
            ),
            "build_complete_end": (
                previous.get("complete_end", 0) if mode == "append" else 0
            ),
            "build_bytes_read": 0,
            "build_elapsed_ms": 0.0,
            "database_work": {
                "seeded_episode_count": 0,
                "seed_elapsed_ms": 0.0,
            },
            "source_standing": (
                previous.get("source_standing", SourceStanding.UNKNOWN.value)
                if previous.get("active_generation_id")
                else SourceStanding.UNKNOWN.value
            ),
            "active_generation_integrity": (
                "invalid"
                if reason == "derived_loss"
                else previous.get("active_generation_integrity")
            ),
            "freshness": (
                FreshnessStanding.TAIL_VALIDATED.value
                if previous.get("active_generation_id") and mode == "append"
                else FreshnessStanding.STALE.value
                if previous.get("active_generation_id")
                else FreshnessStanding.INCOMPLETE.value
            ),
        },
    )
    if old_staging and old_staging != generation_id:
        with store.write_transaction() as connection:
            store.delete_generation(
                connection, enrollment, member, old_staging
            )
    return updated


def _audit_due(
    enrollment: SourceEnrollment,
    member: SourceMember,
    state: dict,
    now: datetime,
) -> bool:
    audit = state.get("integrity_audit") or {}
    if audit.get("in_progress"):
        return True
    if state.get("implementation_version") != get_adapter(
        enrollment.adapter
    ).implementation_version:
        return True
    generation = _stat(member)
    complete_end = state.get("complete_end", 0)
    if (
        state.get("freshness") == FreshnessStanding.TAIL_VALIDATED.value
        and generation is not None
        and generation["size"] > complete_end
        and audit.get("prefix_validated_through") == complete_end
        and generation == state.get("member_generation")
    ):
        return False
    if state.get("freshness") != FreshnessStanding.CURRENT.value:
        return True
    validated = _parse_timestamp(state.get("validated_at"))
    expired = validated is None or (
        now - validated
    ).total_seconds() > enrollment.full_validation_max_age_seconds
    if expired or generation is None:
        return True
    if generation["size"] > complete_end:
        return False
    return generation != state.get("member_generation")


def _start_audit(
    store: SQLiteStore,
    enrollment: SourceEnrollment,
    member: SourceMember,
    state: dict,
) -> dict:
    generation = _stat(member)
    previous_audit = state.get("integrity_audit") or {}
    shrunk = bool(
        generation is not None
        and generation["size"] < state.get("complete_end", 0)
    )
    return store.compare_and_swap_state(
        enrollment,
        member.member_id,
        state,
        {
            "freshness": (
                FreshnessStanding.STALE.value
                if shrunk
                else FreshnessStanding.TAIL_VALIDATED.value
            ),
            "integrity_audit": {
                "in_progress": True,
                "offset": 0,
                "cursor": {"byte_offset": 0, "adapter_state": {}},
                "chain_digest": "",
                "episode_count": 0,
                "target_end": state.get("complete_end", 0),
                "start_generation": generation,
                "bytes_read": 0,
                "elapsed_ms": 0.0,
                "restart_count": previous_audit.get("restart_count", 0),
                "trusted_chain_digest": previous_audit.get(
                    "trusted_chain_digest"
                ),
                "trusted_episode_count": previous_audit.get(
                    "trusted_episode_count"
                ),
            },
        },
    )


def _reconcile_audit(
    store: SQLiteStore,
    enrollment: SourceEnrollment,
    member: SourceMember,
    state: dict,
    budget: WorkBudget,
) -> None:
    audit = state.get("integrity_audit") or {}
    if not audit.get("in_progress"):
        state = _start_audit(store, enrollment, member, state)
        audit = state["integrity_audit"]
    if budget.exhausted:
        return

    cursor = _cursor(audit.get("cursor"))
    target_end = audit["target_end"]
    started = time.monotonic()
    chunk = get_adapter(enrollment.adapter).scan_chunk(
        enrollment,
        member,
        cursor,
        min(budget.remaining, max(1, target_end - cursor.byte_offset)),
    )
    elapsed_ms = (time.monotonic() - started) * 1000
    budget.charge(chunk.bytes_read)
    chain = audit.get("chain_digest", "")
    for episode in chunk.episodes:
        chain = extend_chain(chain, episode.identity.episode_ref)

    documents = store.generation_documents(
        enrollment,
        member.member_id,
        state["active_generation_id"],
        start=audit.get("offset", 0),
        end=chunk.next_cursor.byte_offset,
    )
    expected = tuple(
        (
            document["episode_ref"],
            document["source_position"]["start"],
            document["source_position"]["end"],
        )
        for document in documents
    )
    actual = tuple(
        (
            episode.identity.episode_ref,
            episode.source_position["start"],
            episode.source_position["end"],
        )
        for episode in chunk.episodes
    )
    updated_audit = {
        **audit,
        "offset": chunk.next_cursor.byte_offset,
        "cursor": _cursor_dict(chunk.next_cursor),
        "chain_digest": chain,
        "episode_count": audit.get("episode_count", 0) + len(chunk.episodes),
        "bytes_read": audit.get("bytes_read", 0) + chunk.bytes_read,
        "elapsed_ms": audit.get("elapsed_ms", 0.0) + elapsed_ms,
    }
    source_failed = (
        chunk.source_standing is not SourceStanding.AVAILABLE
        or chunk.error_position is not None
    )
    if source_failed:
        store.compare_and_swap_state(
            enrollment,
            member.member_id,
            state,
            {
                "integrity_audit": updated_audit,
                "source_standing": chunk.source_standing.value,
                "error_position": chunk.error_position,
                "freshness": (
                    FreshnessStanding.UNAVAILABLE.value
                    if chunk.source_standing
                    in {SourceStanding.MISSING, SourceStanding.UNAVAILABLE}
                    else FreshnessStanding.UNKNOWN.value
                ),
            },
        )
        return

    audit_complete = chunk.next_cursor.byte_offset >= target_end
    mismatch = expected != actual or chunk.observed_end < target_end
    if audit_complete:
        mismatch = mismatch or (
            updated_audit["chain_digest"] != audit.get("trusted_chain_digest")
            or updated_audit["episode_count"] != audit.get("trusted_episode_count")
        )
    if mismatch:
        stale = store.compare_and_swap_state(
            enrollment,
            member.member_id,
            state,
            {
                "integrity_audit": updated_audit,
                "observed_end": chunk.observed_end,
                "source_standing": chunk.source_standing.value,
                "freshness": FreshnessStanding.STALE.value,
                "error_position": chunk.error_position,
            },
        )
        _begin_build(store, enrollment, member, stale, "source_content")
        return
    if not audit_complete:
        store.compare_and_swap_state(
            enrollment,
            member.member_id,
            state,
            {
                "integrity_audit": updated_audit,
                "source_standing": chunk.source_standing.value,
                "freshness": FreshnessStanding.TAIL_VALIDATED.value,
            },
        )
        return

    finished_generation = _stat(member)
    if finished_generation != audit.get("start_generation"):
        restarted = store.compare_and_swap_state(
            enrollment,
            member.member_id,
            state,
            {
                "integrity_audit": {
                    **updated_audit,
                    "in_progress": False,
                    "restart_count": updated_audit.get("restart_count", 0) + 1,
                }
            },
        )
        _start_audit(store, enrollment, member, restarted)
        return
    completed_audit = {
        **updated_audit,
        "in_progress": False,
        "prefix_validated_through": target_end,
        "trusted_chain_digest": updated_audit["chain_digest"],
        "trusted_episode_count": updated_audit["episode_count"],
    }
    source_has_tail = chunk.observed_end > target_end
    store.compare_and_swap_state(
        enrollment,
        member.member_id,
        state,
        {
            "integrity_audit": completed_audit,
            "validated_at": _timestamp(budget.now),
            "member_generation": finished_generation,
            "implementation_version": get_adapter(
                enrollment.adapter
            ).implementation_version,
            "source_standing": chunk.source_standing.value,
            "observed_end": chunk.observed_end,
            "freshness": (
                FreshnessStanding.TAIL_VALIDATED.value
                if source_has_tail
                else FreshnessStanding.CURRENT.value
            ),
            "error_position": None,
        },
    )


def _supersession_observations(
    store: SQLiteStore,
    enrollment: SourceEnrollment,
    member: SourceMember,
    old_generation: str,
    new_generation: str,
    now: datetime,
) -> tuple[dict, ...]:
    old_by_event: dict[tuple[str, str], str] = {}
    for document in store.generation_documents(
        enrollment, member.member_id, old_generation
    ):
        reference = EpisodeReference.parse(document["episode_ref"])
        old_by_event[(reference.native_session_id, reference.event_token)] = (
            document["episode_ref"]
        )
    observations = []
    for document in store.generation_documents(
        enrollment, member.member_id, new_generation
    ):
        new_ref = document["episode_ref"]
        reference = EpisodeReference.parse(new_ref)
        old_ref = old_by_event.get(
            (reference.native_session_id, reference.event_token)
        )
        if not old_ref or old_ref == new_ref:
            continue
        observations.append(
            {
                "observation_key": hashlib.sha256(
                    f"{old_ref}\0{new_ref}".encode("utf-8")
                ).hexdigest(),
                "corpus_id": enrollment.corpus_id,
                "source_id": enrollment.source_id,
                "member_id": member.member_id,
                "event_token": reference.event_token,
                "old_ref": old_ref,
                "new_ref": new_ref,
                "reason": "same_event_content_changed",
                "detected_at": _timestamp(now),
            }
        )
    return tuple(sorted(observations, key=lambda item: item["observation_key"]))


def _with_one_state_retry(operation) -> None:
    try:
        operation()
    except SQLiteStateConflict:
        try:
            operation()
        except SQLiteStateConflict as exc:
            raise ProviderUnavailable(
                "concurrent SQLite reconciliation did not converge"
            ) from exc


def _reconcile_member(
    store: SQLiteStore,
    enrollment: SourceEnrollment,
    member: SourceMember,
    budget: WorkBudget,
) -> None:
    state = store.member_state(enrollment, member.member_id)
    if not supports_semantic_versions(
        enrollment.adapter,
        boundary_version=enrollment.boundary_version,
        canonicalization_version=enrollment.canonicalization_version,
    ):
        store.compare_and_swap_state(
            enrollment,
            member.member_id,
            state,
            {
                "source_standing": SourceStanding.UNSUPPORTED_ADAPTER.value,
                "freshness": FreshnessStanding.UNAVAILABLE.value,
                "declared_canonicalization_version": (
                    enrollment.canonicalization_version
                ),
                "declared_boundary_version": enrollment.boundary_version,
            },
        )
        return
    if state is None:
        if budget.exhausted:
            return
        state = _begin_build(store, enrollment, member, state, "initial")
    elif state.get("build_generation_id"):
        if (
            state.get("build_canonicalization_version")
            != enrollment.canonicalization_version
            or state.get("build_boundary_version") != enrollment.boundary_version
        ):
            state = _begin_build(
                store, enrollment, member, state, "semantic_version"
            )
        elif _stat(member) != state.get("build_source_snapshot"):
            state = _begin_build(store, enrollment, member, state, "source_drift")
    elif not state.get("build_generation_id"):
        if not state.get("active_generation_id"):
            if budget.exhausted:
                return
            state = _begin_build(store, enrollment, member, state, "initial")
        elif (
            state.get("canonicalization_version")
            != enrollment.canonicalization_version
            or state.get("boundary_version") != enrollment.boundary_version
        ):
            state = _begin_build(
                store, enrollment, member, state, "semantic_version"
            )
        elif state.get("active_generation_integrity") == "invalid":
            if budget.exhausted:
                return
            state = _begin_build(store, enrollment, member, state, "derived_loss")
        else:
            generation = _stat(member)
            if _audit_due(enrollment, member, state, budget.now):
                _reconcile_audit(store, enrollment, member, state, budget)
                return
            if generation is None or generation["size"] <= state.get(
                "complete_end", 0
            ):
                return
            if budget.exhausted:
                return
            state = _begin_build(store, enrollment, member, state, "append")

    if budget.exhausted:
        return

    generation_id = state["staging_generation_id"]
    if state.get("build_mode") == "append" and not state.get("build_seeded"):
        try:
            with store.write_transaction() as connection:
                state, _, _ = store.seed_staging_generation(
                    connection,
                    enrollment,
                    member,
                    state["active_generation_id"],
                    generation_id,
                    expected_state=state,
                    state_values={"build_seeded": True},
                )
        except SQLiteDocumentConflict:
            _begin_build(store, enrollment, member, state, "derived_loss")
            return

    adapter = get_adapter(enrollment.adapter)
    started = time.monotonic()
    chunk = adapter.scan_chunk(
        enrollment, member, _cursor(state.get("build_cursor")), budget.remaining
    )
    elapsed_ms = (time.monotonic() - started) * 1000
    budget.charge(chunk.bytes_read)
    if _stat(member) != state.get("build_source_snapshot"):
        _begin_build(store, enrollment, member, state, "source_drift")
        return
    chain = state.get("build_chain_digest", "")
    for episode in chunk.episodes:
        chain = extend_chain(chain, episode.identity.episode_ref)
    if chunk.source_standing in {
        SourceStanding.MISSING,
        SourceStanding.UNAVAILABLE,
    }:
        building_freshness = FreshnessStanding.UNAVAILABLE.value
    elif (
        chunk.source_standing is not SourceStanding.AVAILABLE
        or chunk.error_position is not None
    ):
        building_freshness = FreshnessStanding.UNKNOWN.value
    elif state.get("active_generation_id") and state.get("build_mode") != "append":
        building_freshness = FreshnessStanding.STALE.value
    elif state.get("active_generation_id"):
        building_freshness = (
            FreshnessStanding.INCOMPLETE.value
            if chunk.exhausted
            or chunk.freshness is FreshnessStanding.INCOMPLETE
            else FreshnessStanding.TAIL_VALIDATED.value
        )
    else:
        building_freshness = FreshnessStanding.INCOMPLETE.value
    state_values = {
            "build_cursor": _cursor_dict(chunk.next_cursor),
            "build_chain_digest": chain,
            "staging_episode_count": state.get("staging_episode_count", 0)
            + len(chunk.episodes),
            "build_observed_end": chunk.observed_end,
            "build_complete_end": chunk.complete_end,
            "build_bytes_read": state.get("build_bytes_read", 0)
            + chunk.bytes_read,
            "build_elapsed_ms": state.get("build_elapsed_ms", 0.0) + elapsed_ms,
            "source_standing": chunk.source_standing.value,
            "freshness": building_freshness,
            "error_position": chunk.error_position,
            "database_work": state.get(
                "database_work",
                {"seeded_episode_count": 0, "seed_elapsed_ms": 0.0},
            ),
    }
    with store.write_transaction() as connection:
        state, _ = store.write_staging_chunk(
            connection,
            enrollment,
            member,
            generation_id,
            chunk.episodes,
            expected_state=state,
            state_values=state_values,
        )
    complete = (
        not chunk.exhausted
        and chunk.source_standing is SourceStanding.AVAILABLE
        and chunk.freshness is FreshnessStanding.CURRENT
        and chunk.error_position is None
        and chunk.complete_end == chunk.observed_end
    )
    if not complete:
        return
    if _stat(member) != state.get("build_source_snapshot"):
        _begin_build(store, enrollment, member, state, "source_drift")
        return
    audit = {
        "offset": chunk.complete_end,
        "cursor": _cursor_dict(chunk.next_cursor),
        "chain_digest": chain,
        "episode_count": state["staging_episode_count"],
        "bytes_read": state.get("build_bytes_read", 0),
        "elapsed_ms": state.get("build_elapsed_ms", 0.0),
        "restart_count": 0,
        "trusted_chain_digest": chain,
        "trusted_episode_count": state["staging_episode_count"],
    }
    old_generation = state.get("active_generation_id")
    observations = (
        _supersession_observations(
            store,
            enrollment,
            member,
            old_generation,
            generation_id,
            budget.now,
        )
        if old_generation and state.get("build_mode") != "append"
        else ()
    )
    final_freshness = (
        FreshnessStanding.TAIL_VALIDATED.value
        if state.get("build_mode") == "append"
        else FreshnessStanding.CURRENT.value
    )
    validated_at = (
        state.get("validated_at")
        if state.get("build_mode") == "append"
        else _timestamp(budget.now)
    )
    with store.write_transaction() as connection:
        store.activate_generation(
            connection,
            enrollment,
            member,
            generation_id,
            expected_count=state["staging_episode_count"],
            expected_state=state,
            state_values={
                "build_generation_id": None,
                "build_mode": None,
                "build_reason": None,
                "build_cursor": None,
                "build_chain_digest": None,
                "build_seeded": None,
                "build_canonicalization_version": None,
                "build_boundary_version": None,
                "build_source_snapshot": None,
                "build_observed_end": None,
                "build_complete_end": None,
                "build_bytes_read": None,
                "build_elapsed_ms": None,
                "tail_cursor": _cursor_dict(chunk.next_cursor),
                "integrity_audit": audit,
                "validated_at": validated_at,
                "observed_end": chunk.observed_end,
                "complete_end": chunk.complete_end,
                "member_generation": state["build_source_snapshot"],
                "implementation_version": adapter.implementation_version,
                "source_standing": chunk.source_standing.value,
                "error_position": chunk.error_position,
                "freshness": final_freshness,
            },
            supersession_observations=observations,
        )
        if old_generation and old_generation != generation_id:
            store.delete_generation(
                connection, enrollment, member, old_generation
            )


def _member_standing(
    state: dict | None,
    member_id: str,
    now: datetime,
    *,
    missing: bool = False,
) -> dict:
    state = state or {}
    integrity = state.get("integrity_audit", {})
    validated_at = state.get("validated_at")
    validated = (
        datetime.fromisoformat(validated_at.replace("Z", "+00:00"))
        if validated_at
        else None
    )
    age = max(0.0, (now - validated).total_seconds()) if validated else None
    return {
        "member_id": member_id,
        "source_standing": (
            SourceStanding.MISSING.value
            if missing
            else state.get("source_standing", SourceStanding.UNKNOWN.value)
        ),
        "index_standing": (
            "available"
            if state.get("active_generation_id")
            and state.get("source_standing")
            != SourceStanding.UNSUPPORTED_ADAPTER.value
            and state.get("active_generation_integrity") != "invalid"
            else "rebuilding"
            if state.get("source_standing") == SourceStanding.AVAILABLE.value
            and state.get("staging_generation_id")
            else "unavailable"
        ),
        "freshness": (
            FreshnessStanding.UNAVAILABLE.value
            if missing
            else state.get("freshness", FreshnessStanding.UNKNOWN.value)
        ),
        "indexed_through": {
            "kind": "byte_offset",
            "value": (
                state.get("complete_end", 0)
                if state.get("active_generation_id")
                else state.get("build_complete_end", 0)
            ),
        },
        "observed_source_end": {
            "kind": "byte_offset",
            "value": (
                state.get("observed_end", 0)
                if state.get("active_generation_id")
                else state.get("build_observed_end", 0)
            ),
        },
        "error_position": state.get("error_position"),
        "integrity": {
            "basis": "full_digest",
            "validated_at": validated_at,
            "validation_age_seconds": age,
            "audit_offset": integrity.get("offset", 0),
            "chain_digest": integrity.get("chain_digest", ""),
            "bytes_read": integrity.get("bytes_read", 0),
            "elapsed_ms": integrity.get("elapsed_ms", 0.0),
            "restart_count": integrity.get("restart_count", 0),
        },
        "database_work": state.get(
            "database_work",
            {"seeded_episode_count": 0, "seed_elapsed_ms": 0.0},
        ),
    }


def _source_standing(
    store: SQLiteStore,
    enrollment: SourceEnrollment,
    members: tuple[SourceMember, ...],
    now: datetime,
) -> dict:
    states = {state["member_id"]: state for state in store.source_states(enrollment)}
    try:
        enrollment.locator.stat()
    except FileNotFoundError:
        source_set_standing = SourceStanding.MISSING.value
    except OSError:
        source_set_standing = SourceStanding.UNAVAILABLE.value
    else:
        source_set_standing = SourceStanding.AVAILABLE.value
    adapter = get_adapter(enrollment.adapter)
    live_ids = {member.member_id for member in members}
    member_reports = [
        _member_standing(states.get(member.member_id), member.member_id, now)
        for member in members
    ]
    member_reports.extend(
        _member_standing(state, member_id, now, missing=True)
        for member_id, state in states.items()
        if member_id not in live_ids
    )
    member_reports.sort(key=lambda item: item["member_id"])
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
    store: SQLiteStore, registry: EnrollmentRegistry, budget: WorkBudget
) -> ReconcileReport:
    started = time.monotonic()
    initial_bytes = budget.bytes_read
    sources = tuple(
        sorted(
            (source for source in registry.sources if source.enabled),
            key=lambda source: (source.corpus_id, source.source_id),
        )
    )
    discovered = {
        (source.corpus_id, source.source_id): tuple(
            sorted(
                get_adapter(source.adapter).members(source),
                key=lambda member: member.member_id,
            )
        )
        for source in sources
    }
    for source in sources:
        for member in discovered[(source.corpus_id, source.source_id)]:
            _with_one_state_retry(
                lambda source=source, member=member: _reconcile_member(
                    store, source, member, budget
                )
            )

    corpus_reports = []
    for corpus_id in sorted(registry.known_corpora):
        corpus_reports.append(
            {
                "corpus_id": corpus_id,
                "sources": tuple(
                    _source_standing(
                        store,
                        source,
                        discovered[(source.corpus_id, source.source_id)],
                        budget.now,
                    )
                    for source in sources
                    if source.corpus_id == corpus_id
                ),
            }
        )
    return ReconcileReport(
        tuple(corpus_reports),
        budget.bytes_read - initial_bytes,
        (time.monotonic() - started) * 1000,
        budget.exhausted,
    )
