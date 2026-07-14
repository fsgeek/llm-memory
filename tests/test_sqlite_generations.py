from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from llm_memory.adapters import EpisodeRecord, SourceMember
from llm_memory.contract import EpisodeBody, build_identity
from llm_memory.enrollment import SourceEnrollment
from llm_memory.sqlite_store import (
    SQLiteDocumentConflict,
    SQLiteStateConflict,
    SQLiteStore,
)


@pytest.fixture
def sqlite_store(tmp_path):
    store = SQLiteStore(tmp_path / "episodes.sqlite3", busy_timeout_ms=20)
    store.ensure()
    return store


@pytest.fixture
def enrollment(tmp_path):
    return SourceEnrollment(
        corpus_id="corpus-a",
        source_id="source-a",
        adapter="gateway_jsonl",
        boundary_version=3,
        canonicalization_version=4,
        locator=tmp_path / "source.jsonl",
        enabled=True,
        full_validation_max_age_seconds=86400,
    )


@pytest.fixture
def member(tmp_path):
    return SourceMember("member-a", tmp_path / "member.jsonl")


def _episode(enrollment: SourceEnrollment, event_token: str) -> EpisodeRecord:
    body = EpisodeBody(
        timestamp="2026-07-14T10:00:00Z",
        model="model-a",
        user_message=f"question {event_token}",
        response=f"answer {event_token}",
        state={"status": "observed"},
        activity_log=[{"action": "read"}],
        adapter_fields={"messages_full": [{"role": "user"}]},
    )
    return EpisodeRecord(
        identity=build_identity(
            corpus_id=enrollment.corpus_id,
            source_id=enrollment.source_id,
            native_session_id="session-a",
            event_token=event_token,
            canonicalization_version=enrollment.canonicalization_version,
            boundary_version=enrollment.boundary_version,
            body=body,
        ),
        body=body,
        native_event_id=event_token,
        source_position={"start": 11, "end": 29},
        state_text="status observed",
    )


@pytest.fixture
def episodes(enrollment):
    return (_episode(enrollment, "event-a"), _episode(enrollment, "event-b"))


def _stage_generation(sqlite_store, enrollment, member, generation_id, expected=None):
    return sqlite_store.compare_and_swap_state(
        enrollment,
        member.member_id,
        expected,
        {"staging_generation_id": generation_id, "freshness": "incomplete"},
    )


def test_state_compare_and_swap_rejects_stale_revision(
    sqlite_store, enrollment, member
):
    original = sqlite_store.compare_and_swap_state(
        enrollment, member.member_id, None, {"active_generation_id": None}
    )
    updated = sqlite_store.compare_and_swap_state(
        enrollment,
        member.member_id,
        original,
        {"freshness_standing": "incomplete"},
    )

    with pytest.raises(SQLiteStateConflict):
        sqlite_store.compare_and_swap_state(
            enrollment,
            member.member_id,
            original,
            {"freshness_standing": "current"},
        )

    assert updated["revision"] == 2
    assert sqlite_store.member_state(enrollment, member.member_id) == updated


def test_state_json_is_canonical_and_source_states_are_member_sorted(
    sqlite_store, enrollment, member
):
    second = sqlite_store.compare_and_swap_state(
        enrollment, "member-z", None, {"z": 1, "a": {"y": 2, "x": 1}}
    )
    first = sqlite_store.compare_and_swap_state(
        enrollment, member.member_id, None, {"freshness": "incomplete"}
    )

    with sqlite_store.connect() as connection:
        stored = connection.execute(
            "SELECT state_json FROM source_states WHERE member_id = ?",
            ("member-z",),
        ).fetchone()[0]

    expected_json = json.dumps(
        {key: value for key, value in second.items() if key != "revision"},
        sort_keys=True,
        separators=(",", ":"),
    )
    assert stored == expected_json
    assert sqlite_store.source_states(enrollment) == (first, second)


def test_generation_writes_are_immutable_and_identical_replay_is_idempotent(
    sqlite_store, enrollment, member, episodes
):
    with sqlite_store.write_transaction() as connection:
        assert (
            sqlite_store.write_generation(
                connection, enrollment, member, "generation-a", episodes
            )
            == 2
        )
        assert (
            sqlite_store.write_generation(
                connection, enrollment, member, "generation-a", episodes
            )
            == 2
        )
        assert sqlite_store.generation_count(connection, "generation-a") == 2

    conflicting = replace(
        episodes[0], body=replace(episodes[0].body, response="different response")
    )
    with pytest.raises(SQLiteDocumentConflict):
        with sqlite_store.write_transaction() as connection:
            sqlite_store.write_generation(
                connection, enrollment, member, "generation-a", (conflicting,)
            )


def test_generation_replay_detects_corrupted_materialized_columns(
    sqlite_store, enrollment, member, episodes
):
    with sqlite_store.write_transaction() as connection:
        sqlite_store.write_generation(
            connection, enrollment, member, "generation-a", (episodes[0],)
        )
        connection.execute(
            "UPDATE episode_documents SET response = ? WHERE generation_id = ?",
            ("corrupted response", "generation-a"),
        )

    with pytest.raises(SQLiteDocumentConflict):
        with sqlite_store.write_transaction() as connection:
            sqlite_store.write_generation(
                connection, enrollment, member, "generation-a", (episodes[0],)
            )


def test_generation_document_preserves_identity_evidence_and_search_text(
    sqlite_store, enrollment, member, episodes
):
    episode = episodes[0]
    with sqlite_store.write_transaction() as connection:
        sqlite_store.write_generation(
            connection, enrollment, member, "generation-a", (episode,)
        )
        row = connection.execute(
            "SELECT * FROM episode_documents WHERE generation_id = ?",
            ("generation-a",),
        ).fetchone()

    document = json.loads(row["document_json"])
    assert document == document | {
        "storage_key": row["storage_key"],
        "episode_ref": episode.identity.episode_ref,
        "reference_key": row["reference_key"],
        "corpus_id": enrollment.corpus_id,
        "source_id": enrollment.source_id,
        "member_id": member.member_id,
        "generation_id": "generation-a",
        "canonicalization_version": 4,
        "boundary_version": 3,
        "body_digest": episode.identity.body_digest,
        "native_event_id": "event-a",
        "source_position": {"start": 11, "end": 29},
        "timestamp": "2026-07-14T10:00:00Z",
        "model": "model-a",
        "user_message": "question event-a",
        "response": "answer event-a",
        "state": {"status": "observed"},
        "activity_log": [{"action": "read"}],
        "adapter_fields": {"messages_full": [{"role": "user"}]},
        "state_text": "status observed",
    }


def test_seed_generation_clones_active_rows_and_reports_database_work(
    sqlite_store, enrollment, member, episodes
):
    expected = _stage_generation(
        sqlite_store, enrollment, member, "generation-active"
    )
    with sqlite_store.write_transaction() as connection:
        sqlite_store.write_generation(
            connection, enrollment, member, "generation-active", episodes
        )
        sqlite_store.activate_generation(
            connection,
            enrollment,
            member,
            "generation-active",
            expected_count=2,
            expected_state=expected,
        )

    with sqlite_store.write_transaction() as connection:
        copied_count, database_elapsed_ms = sqlite_store.seed_generation(
            connection,
            enrollment,
            member,
            "generation-active",
            "generation-staging",
        )
        assert copied_count == 2
        assert database_elapsed_ms >= 0
        assert sqlite_store.generation_count(connection, "generation-staging") == 2
        generations = connection.execute(
            "SELECT generation_id, document_json FROM episode_documents "
            "ORDER BY generation_id, episode_ref"
        ).fetchall()

    assert {row["generation_id"] for row in generations} == {
        "generation-active",
        "generation-staging",
    }
    assert all(
        json.loads(row["document_json"])["generation_id"] == row["generation_id"]
        for row in generations
    )


def test_seed_generation_rejects_a_non_active_source_generation(
    sqlite_store, enrollment, member, episodes
):
    expected = _stage_generation(
        sqlite_store, enrollment, member, "generation-active"
    )
    with sqlite_store.write_transaction() as connection:
        sqlite_store.write_generation(
            connection, enrollment, member, "generation-active", episodes
        )
        sqlite_store.activate_generation(
            connection,
            enrollment,
            member,
            "generation-active",
            expected_count=2,
            expected_state=expected,
        )
        sqlite_store.write_generation(
            connection, enrollment, member, "generation-inactive", episodes
        )
        with pytest.raises(SQLiteStateConflict, match="not active"):
            sqlite_store.seed_generation(
                connection,
                enrollment,
                member,
                "generation-inactive",
                "generation-staging",
            )

        assert sqlite_store.generation_count(connection, "generation-staging") == 0


def test_activation_verifies_documents_and_fts_before_publishing_state(
    sqlite_store, enrollment, member, episodes
):
    expected = _stage_generation(sqlite_store, enrollment, member, "generation-a")
    with sqlite_store.write_transaction() as connection:
        sqlite_store.write_generation(
            connection, enrollment, member, "generation-a", episodes
        )
        assert sqlite_store.verify_generation(
            connection, "generation-a", expected_count=2
        )
        sqlite_store.activate_generation(
            connection,
            enrollment,
            member,
            "generation-a",
            expected_count=2,
            expected_state=expected,
        )

    state = sqlite_store.member_state(enrollment, member.member_id)
    assert state == state | {
        "active_generation_id": "generation-a",
        "staging_generation_id": None,
        "episode_count": 2,
        "active_generation_integrity": "valid",
        "freshness": "current",
        "canonicalization_version": 4,
        "boundary_version": 3,
        "revision": 2,
    }


def test_activation_rejects_wrong_document_or_fts_count_without_state(
    sqlite_store, enrollment, member, episodes
):
    expected = _stage_generation(sqlite_store, enrollment, member, "generation-a")
    with sqlite_store.write_transaction() as connection:
        sqlite_store.write_generation(
            connection, enrollment, member, "generation-a", episodes
        )
        rowid = connection.execute(
            "SELECT rowid FROM episode_documents ORDER BY rowid LIMIT 1"
        ).fetchone()[0]
        connection.execute("DELETE FROM episode_fts WHERE rowid = ?", (rowid,))
        assert not sqlite_store.verify_generation(
            connection, "generation-a", expected_count=2
        )
        with pytest.raises(SQLiteDocumentConflict):
            sqlite_store.activate_generation(
                connection,
                enrollment,
                member,
                "generation-a",
                expected_count=2,
                expected_state=expected,
            )

    assert sqlite_store.member_state(enrollment, member.member_id) == expected


def test_activation_rejects_a_generation_owned_by_another_member(
    sqlite_store, enrollment, member, episodes
):
    other_member = SourceMember("member-b", Path("/unused"))
    expected = _stage_generation(
        sqlite_store, enrollment, member, "generation-foreign"
    )
    with sqlite_store.write_transaction() as connection:
        sqlite_store.write_generation(
            connection,
            enrollment,
            other_member,
            "generation-foreign",
            (episodes[0],),
        )
        with pytest.raises(SQLiteDocumentConflict, match="incomplete or unindexed"):
            sqlite_store.activate_generation(
                connection,
                enrollment,
                member,
                "generation-foreign",
                expected_count=1,
                expected_state=expected,
            )

    assert sqlite_store.member_state(enrollment, member.member_id) == expected


def test_stale_publisher_cannot_replace_a_newer_active_generation(
    sqlite_store, enrollment, member, episodes
):
    with sqlite_store.write_transaction() as connection:
        sqlite_store.write_generation(
            connection, enrollment, member, "generation-old", episodes
        )
        sqlite_store.write_generation(
            connection, enrollment, member, "generation-new", episodes
        )

    stale_expected = sqlite_store.compare_and_swap_state(
        enrollment,
        member.member_id,
        None,
        {"staging_generation_id": "generation-old", "freshness": "incomplete"},
    )
    newer_expected = sqlite_store.compare_and_swap_state(
        enrollment,
        member.member_id,
        stale_expected,
        {"staging_generation_id": "generation-new", "freshness": "incomplete"},
    )
    with sqlite_store.write_transaction() as connection:
        sqlite_store.activate_generation(
            connection,
            enrollment,
            member,
            "generation-new",
            expected_count=2,
            expected_state=newer_expected,
        )

    newer_active = sqlite_store.member_state(enrollment, member.member_id)
    with pytest.raises(SQLiteStateConflict):
        with sqlite_store.write_transaction() as connection:
            sqlite_store.activate_generation(
                connection,
                enrollment,
                member,
                "generation-old",
                expected_count=2,
                expected_state=stale_expected,
            )

    assert sqlite_store.member_state(enrollment, member.member_id) == newer_active
    assert newer_active == newer_active | {
        "active_generation_id": "generation-new",
        "staging_generation_id": None,
        "freshness": "current",
        "revision": 3,
    }


def test_incomplete_generation_is_not_active_after_rollback(
    sqlite_store, enrollment, member, episodes
):
    expected = _stage_generation(sqlite_store, enrollment, member, "generation-a")
    with pytest.raises(RuntimeError, match="crash before commit"):
        with sqlite_store.write_transaction() as connection:
            sqlite_store.write_generation(
                connection, enrollment, member, "generation-a", episodes
            )
            sqlite_store.activate_generation(
                connection,
                enrollment,
                member,
                "generation-a",
                expected_count=len(episodes),
                expected_state=expected,
            )
            raise RuntimeError("crash before commit")

    assert sqlite_store.member_state(enrollment, member.member_id) == expected
    with sqlite_store.read_transaction() as connection:
        assert sqlite_store.generation_count(connection, "generation-a") == 0


def test_delete_generation_preserves_active_and_removes_inactive_fts_rows(
    sqlite_store, enrollment, member, episodes
):
    expected = _stage_generation(
        sqlite_store, enrollment, member, "generation-active"
    )
    with sqlite_store.write_transaction() as connection:
        sqlite_store.write_generation(
            connection, enrollment, member, "generation-active", episodes
        )
        sqlite_store.activate_generation(
            connection,
            enrollment,
            member,
            "generation-active",
            expected_count=2,
            expected_state=expected,
        )
        sqlite_store.write_generation(
            connection, enrollment, member, "generation-old", episodes
        )
        assert (
            sqlite_store.delete_generation(
                connection, enrollment, member, "generation-active"
            )
            == 0
        )
        assert (
            sqlite_store.delete_generation(
                connection, enrollment, member, "generation-old"
            )
            == 2
        )
        assert sqlite_store.generation_count(connection, "generation-active") == 2
        assert sqlite_store.generation_count(connection, "generation-old") == 0
        assert connection.execute(
            "SELECT count(*) FROM episode_fts WHERE generation_id = ?",
            ("generation-old",),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutation",
    (
        lambda store, connection, enrollment, member, episodes: store.insert_episode(
            connection,
            {
                "storage_key": "storage-a",
                "corpus_id": enrollment.corpus_id,
                "source_id": enrollment.source_id,
                "member_id": member.member_id,
                "generation_id": "generation-a",
                "episode_ref": episodes[0].identity.episode_ref,
                "reference_key": "reference-a",
                "timestamp": episodes[0].body.timestamp,
                "user_message": episodes[0].body.user_message,
                "response": episodes[0].body.response,
                "state_text": episodes[0].state_text,
                "document_json": "{}",
            },
        ),
        lambda store, connection, enrollment, member, episodes: store.delete_episode(
            connection, 1
        ),
        lambda store, connection, enrollment, member, episodes: store.write_generation(
            connection, enrollment, member, "generation-a", episodes
        ),
        lambda store, connection, enrollment, member, episodes: store.seed_generation(
            connection,
            enrollment,
            member,
            "generation-active",
            "generation-staging",
        ),
        lambda store, connection, enrollment, member, episodes: store.delete_generation(
            connection, enrollment, member, "generation-a"
        ),
        lambda store, connection, enrollment, member, episodes: store.activate_generation(
            connection,
            enrollment,
            member,
            "generation-a",
            expected_count=2,
            expected_state={
                "revision": 1,
                "staging_generation_id": "generation-a",
            },
        ),
    ),
)
def test_connection_mutations_require_an_explicit_transaction(
    sqlite_store, enrollment, member, episodes, mutation
):
    with sqlite_store.connect() as connection:
        with pytest.raises(RuntimeError, match="active explicit transaction"):
            mutation(sqlite_store, connection, enrollment, member, episodes)
