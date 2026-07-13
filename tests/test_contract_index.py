from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from llm_memory.adapters import EpisodeRecord, SourceMember
from llm_memory.contract import EpisodeBody, build_identity, reference_key
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    CONTRACT_VIEW,
    SOURCE_STATES,
    SUPERSESSIONS,
    active_states,
    activate_generation,
    delete_generation,
    ensure_contract_index,
    generation_storage_key,
    write_generation,
)
from llm_memory.db import get_database
from llm_memory.enrollment import SourceEnrollment
from llm_memory.index import EPISODES, VIEW, ensure_index


@pytest.fixture
def contract_storage():
    db = get_database()
    ensure_contract_index(db)
    prefix = f"contract-test-{uuid4().hex}"
    corpus_id = f"{prefix}-corpus"
    sentinel_corpus_id = f"{prefix}-sentinel"
    try:
        yield db, corpus_id, sentinel_corpus_id
    finally:
        for collection_name in (
            CONTRACT_EPISODES,
            SOURCE_STATES,
            SUPERSESSIONS,
        ):
            db.aql.execute(
                """
                FOR doc IN @@collection
                    FILTER STARTS_WITH(doc.corpus_id, @prefix)
                    REMOVE doc IN @@collection
                """,
                bind_vars={
                    "@collection": collection_name,
                    "prefix": prefix,
                },
            )


def _enrollment(corpus_id: str, source_id: str = "source-a") -> SourceEnrollment:
    return SourceEnrollment(
        corpus_id=corpus_id,
        source_id=source_id,
        adapter="gateway_jsonl",
        boundary_version=3,
        canonicalization_version=4,
        locator=Path("/does/not/matter.jsonl"),
        enabled=True,
        full_validation_max_age_seconds=86400,
    )


def _episode(enrollment: SourceEnrollment, event_token: str = "event-a") -> EpisodeRecord:
    body = EpisodeBody(
        timestamp="2026-07-12T18:29:10Z",
        model="model-a",
        user_message="storage question",
        response="storage answer",
        state={"status": "observed"},
        activity_log=[{"action": "read"}],
        adapter_fields={"messages_full": [{"role": "user"}]},
    )
    identity = build_identity(
        corpus_id=enrollment.corpus_id,
        source_id=enrollment.source_id,
        native_session_id="session-a",
        event_token=event_token,
        canonicalization_version=enrollment.canonicalization_version,
        boundary_version=enrollment.boundary_version,
        body=body,
    )
    return EpisodeRecord(
        identity=identity,
        body=body,
        native_event_id=event_token,
        source_position={"start": 11, "end": 29},
        state_text="status observed",
    )


def _state() -> dict:
    return {
        "implementation_version": "adapter-release-7",
        "observed_end": 29,
        "complete_end": 29,
        "member_generation": {"size": 29, "mtime_ns": 123456789},
        "freshness": "current",
        "integrity_audit": {
            "offset": 29,
            "chain_digest": "ab" * 32,
            "restart_count": 0,
        },
        "validated_at": "2026-07-12T18:30:00Z",
    }


def test_ensure_contract_index_is_idempotent_and_isolated_from_legacy_index():
    db = get_database()
    ensure_index(db)
    legacy_count = db.collection(EPISODES).count()

    ensure_contract_index(db)
    ensure_contract_index(db)

    assert all(
        db.has_collection(name)
        for name in (CONTRACT_EPISODES, SOURCE_STATES, SUPERSESSIONS)
    )
    assert {VIEW, CONTRACT_VIEW}.issubset(view["name"] for view in db.views())
    fields = db.view(CONTRACT_VIEW)["links"][CONTRACT_EPISODES]["fields"]
    assert set(fields) == {"user_message", "response", "state_text"}
    assert all(fields[name]["analyzers"] == ["text_en"] for name in fields)
    assert db.collection(EPISODES).count() == legacy_count


def test_generation_documents_preserve_identity_evidence_and_search_text(
    contract_storage,
):
    db, corpus_id, _ = contract_storage
    enrollment = _enrollment(corpus_id)
    member = SourceMember("member-a", Path("/does/not/matter.jsonl"))
    episode = _episode(enrollment)
    generation_id = "generation-a"

    assert write_generation(db, enrollment, member, generation_id, [episode]) == 1

    document = db.collection(CONTRACT_EPISODES).get(
        generation_storage_key(generation_id, episode.identity.episode_ref)
    )
    assert document == document | {
        "_key": generation_storage_key(generation_id, episode.identity.episode_ref),
        "episode_ref": episode.identity.episode_ref,
        "reference_key": reference_key(episode.identity.episode_ref),
        "corpus_id": corpus_id,
        "source_id": enrollment.source_id,
        "member_id": member.member_id,
        "generation_id": generation_id,
        "canonicalization_version": 4,
        "boundary_version": 3,
        "body_digest": episode.identity.body_digest,
        "native_event_id": "event-a",
        "source_position": {"start": 11, "end": 29},
        "timestamp": "2026-07-12T18:29:10Z",
        "model": "model-a",
        "user_message": "storage question",
        "response": "storage answer",
        "state": {"status": "observed"},
        "activity_log": [{"action": "read"}],
        "adapter_fields": {"messages_full": [{"role": "user"}]},
        "state_text": "status observed",
    }


def test_generation_key_allows_active_and_staging_copies_to_coexist(
    contract_storage,
):
    db, corpus_id, _ = contract_storage
    enrollment = _enrollment(corpus_id)
    member = SourceMember("member-a", Path("/unused"))
    episode = _episode(enrollment)

    write_generation(db, enrollment, member, "generation-active", [episode])
    activate_generation(db, enrollment, member, "generation-active", _state())
    write_generation(db, enrollment, member, "generation-staging", [episode])

    documents = list(
        db.aql.execute(
            """
            FOR doc IN @@episodes
                FILTER doc.corpus_id == @corpus_id
                SORT doc.generation_id
                RETURN doc
            """,
            bind_vars={"@episodes": CONTRACT_EPISODES, "corpus_id": corpus_id},
        )
    )
    assert [doc["generation_id"] for doc in documents] == [
        "generation-active",
        "generation-staging",
    ]
    assert len({doc["_key"] for doc in documents}) == 2
    assert len({doc["reference_key"] for doc in documents}) == 1

    state = active_states(db, (corpus_id,))[0]
    assert state["active_generation_id"] == "generation-active"
    assert state["staging_generation_id"] == "generation-staging"


def test_activation_occurs_only_after_complete_generation_write(contract_storage):
    db, corpus_id, _ = contract_storage
    enrollment = _enrollment(corpus_id)
    member = SourceMember("member-a", Path("/unused"))
    episodes = [_episode(enrollment, "event-a"), _episode(enrollment, "event-b")]

    write_generation(db, enrollment, member, "generation-a", episodes)
    assert active_states(db, (corpus_id,)) == ()

    activate_generation(db, enrollment, member, "generation-a", _state())

    states = active_states(db, (corpus_id,))
    assert len(states) == 1
    assert states[0] == states[0] | {
        "corpus_id": corpus_id,
        "source_id": enrollment.source_id,
        "member_id": member.member_id,
        "active_generation_id": "generation-a",
        "staging_generation_id": None,
        "episode_count": 2,
        "canonicalization_version": 4,
        "boundary_version": 3,
        **_state(),
    }


def test_activation_rejects_a_generation_that_was_not_fully_staged(contract_storage):
    db, corpus_id, _ = contract_storage
    enrollment = _enrollment(corpus_id)
    member = SourceMember("member-a", Path("/unused"))

    with pytest.raises(ValueError, match="not fully staged"):
        activate_generation(db, enrollment, member, "never-written", _state())
    assert active_states(db, (corpus_id,)) == ()


def test_generation_can_be_staged_across_bounded_writes(contract_storage):
    db, corpus_id, _ = contract_storage
    enrollment = _enrollment(corpus_id)
    member = SourceMember("member-a", Path("/unused"))

    write_generation(db, enrollment, member, "bounded", [_episode(enrollment, "a")])
    write_generation(db, enrollment, member, "bounded", [_episode(enrollment, "b")])
    activate_generation(db, enrollment, member, "bounded", _state())

    state = active_states(db, (corpus_id,))[0]
    assert state["episode_count"] == 2


def test_generation_writes_are_idempotent_and_count_stored_documents(contract_storage):
    db, corpus_id, _ = contract_storage
    enrollment = _enrollment(corpus_id)
    member = SourceMember("member-a", Path("/unused"))
    episode = _episode(enrollment)

    write_generation(db, enrollment, member, "retryable", [episode])
    write_generation(db, enrollment, member, "retryable", [episode])
    activate_generation(db, enrollment, member, "retryable", _state())

    assert active_states(db, (corpus_id,))[0]["episode_count"] == 1


def test_generation_retry_rejects_conflicting_deterministic_document(contract_storage):
    db, corpus_id, _ = contract_storage
    enrollment = _enrollment(corpus_id)
    member = SourceMember("member-a", Path("/unused"))
    episode = _episode(enrollment)
    conflicting = replace(
        episode,
        body=replace(episode.body, response="conflicting stored body"),
    )
    write_generation(db, enrollment, member, "retryable", [episode])

    with pytest.raises(ValueError, match="conflicting generation document"):
        write_generation(db, enrollment, member, "retryable", [conflicting])


def test_staging_versions_are_distinct_from_active_versions(contract_storage):
    db, corpus_id, _ = contract_storage
    original = _enrollment(corpus_id)
    changed = replace(original, canonicalization_version=5, boundary_version=6)
    member = SourceMember("member-a", Path("/unused"))
    write_generation(db, original, member, "active", [_episode(original)])
    activate_generation(db, original, member, "active", _state())

    write_generation(db, changed, member, "staging", [_episode(changed)])
    state = active_states(db, (corpus_id,))[0]

    assert state["canonicalization_version"] == 4
    assert state["boundary_version"] == 3
    assert state["staging_canonicalization_version"] == 5
    assert state["staging_boundary_version"] == 6


def test_same_staging_generation_rejects_semantic_version_conflict(contract_storage):
    db, corpus_id, _ = contract_storage
    original = _enrollment(corpus_id)
    changed = replace(original, canonicalization_version=5)
    member = SourceMember("member-a", Path("/unused"))
    write_generation(db, original, member, "staging", [_episode(original)])

    with pytest.raises(ValueError, match="staging generation semantic versions conflict"):
        write_generation(db, changed, member, "staging", [_episode(changed)])


def test_active_states_filters_corpora_and_generation_deletion_is_scoped(
    contract_storage,
):
    db, corpus_id, sentinel_corpus_id = contract_storage
    member = SourceMember("member-a", Path("/unused"))
    enrollment = _enrollment(corpus_id)
    sentinel = _enrollment(sentinel_corpus_id)
    episode = _episode(enrollment)
    sentinel_episode = _episode(sentinel)

    write_generation(db, enrollment, member, "old", [episode])
    activate_generation(db, enrollment, member, "old", _state())
    write_generation(db, enrollment, member, "new", [episode])
    activate_generation(db, enrollment, member, "new", _state())
    write_generation(db, sentinel, member, "old", [sentinel_episode])
    activate_generation(db, sentinel, member, "old", _state())

    assert [state["corpus_id"] for state in active_states(db, (corpus_id,))] == [
        corpus_id
    ]
    assert delete_generation(
        db, corpus_id, enrollment.source_id, member.member_id, "old"
    ) == 1
    assert db.collection(CONTRACT_EPISODES).has(
        generation_storage_key("old", sentinel_episode.identity.episode_ref)
    )
    assert active_states(db, (sentinel_corpus_id,))[0]["active_generation_id"] == "old"


def test_delete_generation_does_not_remove_the_active_generation(contract_storage):
    db, corpus_id, _ = contract_storage
    enrollment = _enrollment(corpus_id)
    member = SourceMember("member-a", Path("/unused"))
    episode = _episode(enrollment)

    write_generation(db, enrollment, member, "active", [episode])
    activate_generation(db, enrollment, member, "active", _state())

    assert delete_generation(
        db, corpus_id, enrollment.source_id, member.member_id, "active"
    ) == 0
    assert db.collection(CONTRACT_EPISODES).has(
        generation_storage_key("active", episode.identity.episode_ref)
    )


def test_active_state_reports_missing_stored_generation_population(contract_storage):
    db, corpus_id, _ = contract_storage
    enrollment = _enrollment(corpus_id)
    member = SourceMember("member-a", Path("/unused"))
    episode = _episode(enrollment)
    write_generation(db, enrollment, member, "active", [episode])
    activate_generation(db, enrollment, member, "active", _state())
    db.collection(CONTRACT_EPISODES).delete(
        generation_storage_key("active", episode.identity.episode_ref)
    )

    state = active_states(db, (corpus_id,))[0]

    assert state["stored_episode_count"] == 0
    assert state["active_generation_backed"] is False


def test_generation_storage_key_uses_generation_nul_episode_sha256():
    expected = hashlib.sha256(b"generation-a\0episode://corpus/session/event").hexdigest()
    assert (
        generation_storage_key(
            "generation-a", "episode://corpus/session/event"
        )
        == expected
    )
