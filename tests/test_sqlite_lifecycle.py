from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llm_memory.contract import SearchRequest
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.provider import ProviderUnsupported, PurgeScope
from llm_memory.reconcile import WorkBudget
from llm_memory.sqlite_history import (
    SQLITE_STRATEGY,
    _backed_generations,
    _results,
    encode_fts5_query,
    search_history,
)
from llm_memory.sqlite_lifecycle import measure, purge, remove_provider_file
from llm_memory.sqlite_reconcile import reconcile_registry
from llm_memory.sqlite_store import SQLiteStore


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
DERIVED_CLASSES = frozenset({"episodes", "reconciliation", "supersessions"})


def _write_source(path: Path, response: str) -> bytes:
    data = (
        json.dumps(
            {
                "cycle": 1,
                "user_message": "synthetic lifecycle question",
                "response_text": response,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    path.write_bytes(data)
    return data


def _source(corpus_id: str, source_id: str, locator: Path) -> SourceEnrollment:
    return SourceEnrollment(
        corpus_id=corpus_id,
        source_id=source_id,
        adapter="taste_open_jsonl",
        boundary_version=1,
        canonicalization_version=1,
        locator=locator,
        enabled=True,
        full_validation_max_age_seconds=3600,
    )


@pytest.fixture
def sqlite_store(tmp_path):
    store = SQLiteStore(tmp_path / "episodes.sqlite3", busy_timeout_ms=50)
    store.ensure()
    return store


@pytest.fixture
def populated_fixture(sqlite_store, tmp_path):
    declarations = []
    source_bytes = {}
    for corpus_id, source_id, response in (
        ("local", "selected", "selected lifecycle answer"),
        ("local", "retained", "retained lifecycle answer"),
        ("other", "selected", "other lifecycle answer"),
    ):
        locator = tmp_path / f"{corpus_id}-{source_id}.jsonl"
        source_bytes[(corpus_id, source_id)] = _write_source(locator, response)
        declarations.append(_source(corpus_id, source_id, locator))
    registry = EnrollmentRegistry(tuple(declarations))
    reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))
    with sqlite_store.write_transaction() as connection:
        for enrollment in declarations:
            connection.execute(
                "INSERT INTO supersessions("
                "observation_key, corpus_id, source_id, member_id, event_token, "
                "old_ref, new_ref, reason, detected_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"observation-{enrollment.corpus_id}-{enrollment.source_id}",
                    enrollment.corpus_id,
                    enrollment.source_id,
                    enrollment.source_id,
                    f"event-{enrollment.corpus_id}-{enrollment.source_id}",
                    f"old-{enrollment.corpus_id}-{enrollment.source_id}",
                    f"new-{enrollment.corpus_id}-{enrollment.source_id}",
                    "synthetic replacement",
                    NOW.isoformat(),
                ),
            )
    return {
        "registry": registry,
        "sources": tuple(declarations),
        "source_bytes": source_bytes,
    }


def _counts(store: SQLiteStore) -> dict[str, int]:
    with store.read_transaction() as connection:
        return {
            "episodes": connection.execute(
                "SELECT count(*) FROM episode_documents"
            ).fetchone()[0],
            "fts": connection.execute("SELECT count(*) FROM episode_fts").fetchone()[
                0
            ],
            "reconciliation": connection.execute(
                "SELECT count(*) FROM source_states"
            ).fetchone()[0],
            "supersessions": connection.execute(
                "SELECT count(*) FROM supersessions"
            ).fetchone()[0],
        }


def test_source_purge_counts_selected_classes_and_removes_fts_rows(
    sqlite_store, populated_fixture
):
    before = {
        source.locator: source.locator.read_bytes()
        for source in populated_fixture["sources"]
    }

    report = purge(
        sqlite_store,
        PurgeScope("local", "selected"),
        frozenset({"episodes", "supersessions"}),
    )

    assert report == {"episodes": 1, "supersessions": 1}
    assert _counts(sqlite_store) == {
        "episodes": 2,
        "fts": 2,
        "reconciliation": 3,
        "supersessions": 2,
    }
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize(
    "classes",
    [
        frozenset(),
        frozenset({"unknown"}),
        {"episodes"},
        "episodes",
        DERIVED_CLASSES | frozenset({"source"}),
    ],
)
def test_invalid_purge_classes_fail_before_deletion(
    sqlite_store, populated_fixture, classes
):
    before = _counts(sqlite_store)

    with pytest.raises(ValueError, match="state_classes"):
        purge(sqlite_store, PurgeScope(), classes)

    assert _counts(sqlite_store) == before


@pytest.mark.parametrize(
    "scope",
    [
        None,
        ("local", "selected"),
        PurgeScope(""),
        PurgeScope("local", ""),
        PurgeScope("bad/corpus"),
        PurgeScope("local", "bad/source"),
    ],
)
def test_invalid_purge_scope_fails_before_deletion(
    sqlite_store, populated_fixture, scope
):
    before = _counts(sqlite_store)

    with pytest.raises((TypeError, ValueError), match="scope|corpus_id|source_id"):
        purge(sqlite_store, scope, frozenset({"episodes"}))

    assert _counts(sqlite_store) == before


def test_corpus_and_global_purges_respect_scope(sqlite_store, populated_fixture):
    assert purge(
        sqlite_store,
        PurgeScope("local"),
        frozenset({"reconciliation", "supersessions"}),
    ) == {"reconciliation": 2, "supersessions": 2}
    assert _counts(sqlite_store) == {
        "episodes": 3,
        "fts": 3,
        "reconciliation": 1,
        "supersessions": 1,
    }

    assert purge(sqlite_store, PurgeScope(), DERIVED_CLASSES) == {
        "episodes": 3,
        "reconciliation": 1,
        "supersessions": 1,
    }
    assert _counts(sqlite_store) == {
        "episodes": 0,
        "fts": 0,
        "reconciliation": 0,
        "supersessions": 0,
    }


def test_selected_deletions_and_counts_roll_back_as_one_transaction(
    sqlite_store, populated_fixture
):
    before = _counts(sqlite_store)
    with sqlite_store.write_transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_state_purge BEFORE DELETE ON source_states "
            "BEGIN SELECT RAISE(ABORT, 'synthetic purge failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic purge failure"):
        purge(
            sqlite_store,
            PurgeScope("local", "selected"),
            DERIVED_CLASSES,
        )

    assert _counts(sqlite_store) == before


def test_purging_reconciliation_retains_rows_but_removes_search_authority(
    sqlite_store, populated_fixture
):
    selected = next(
        source
        for source in populated_fixture["sources"]
        if (source.corpus_id, source.source_id) == ("local", "selected")
    )
    assert sqlite_store.active_episode_refs("local", "selected")

    assert purge(
        sqlite_store,
        PurgeScope("local", "selected"),
        frozenset({"reconciliation"}),
    ) == {"reconciliation": 1}

    with sqlite_store.read_transaction() as connection:
        retained = connection.execute(
            "SELECT count(*) FROM episode_documents "
            "WHERE corpus_id = ? AND source_id = ?",
            ("local", "selected"),
        ).fetchone()[0]
        backed = _backed_generations(
            connection, (("local", "selected", selected.source_id),)
        )
        results = _results(
            connection,
            backed,
            encode_fts5_query("lifecycle"),
            10,
        )
    assert retained == 1
    assert sqlite_store.active_episode_refs("local", "selected") == ()
    assert backed == ()
    assert results == []


def test_reconciliation_rebuilds_source_after_derived_purge(
    sqlite_store, populated_fixture
):
    source = next(
        source
        for source in populated_fixture["sources"]
        if (source.corpus_id, source.source_id) == ("local", "selected")
    )
    source_bytes = source.locator.read_bytes()
    assert purge(
        sqlite_store,
        PurgeScope("local", "selected"),
        DERIVED_CLASSES,
    ) == {"episodes": 1, "reconciliation": 1, "supersessions": 1}

    reconcile_registry(
        sqlite_store,
        EnrollmentRegistry((source,)),
        WorkBudget(1_000_000, NOW),
    )

    assert len(sqlite_store.active_episode_refs("local", "selected")) == 1
    assert source.locator.read_bytes() == source_bytes


def test_episode_only_purge_invalidates_then_immediate_search_rebuilds_source(
    sqlite_store, populated_fixture
):
    source = next(
        source
        for source in populated_fixture["sources"]
        if (source.corpus_id, source.source_id) == ("local", "selected")
    )
    original = sqlite_store.member_state(source, source.source_id)
    retained_source = next(
        enrolled
        for enrolled in populated_fixture["sources"]
        if (enrolled.corpus_id, enrolled.source_id) == ("local", "retained")
    )
    retained_state = sqlite_store.member_state(
        retained_source, retained_source.source_id
    )
    source_bytes = source.locator.read_bytes()

    assert purge(
        sqlite_store,
        PurgeScope("local", "selected"),
        frozenset({"episodes"}),
    ) == {"episodes": 1}

    invalidated = sqlite_store.member_state(source, source.source_id)
    assert invalidated["active_generation_id"] == original["active_generation_id"]
    assert invalidated["revision"] == original["revision"] + 1
    assert invalidated["active_generation_integrity"] == "invalid"
    assert invalidated["freshness"] == "stale"
    assert (
        sqlite_store.member_state(retained_source, retained_source.source_id)
        == retained_state
    )
    response = search_history(
        sqlite_store,
        EnrollmentRegistry((source,)),
        SearchRequest.create(
            "selected",
            ["local"],
            strategy=SQLITE_STRATEGY,
        ),
        WorkBudget(1_000_000, NOW),
    )
    recovered = sqlite_store.member_state(source, source.source_id)
    assert recovered["active_generation_id"] != original["active_generation_id"]
    assert recovered["active_generation_integrity"] == "valid"
    assert response["total_matches"] == 1
    assert response["total_standing"] == "exact"
    assert response["returned_count"] == 1
    assert response["results"][0]["corpus_id"] == "local"
    assert source.locator.read_bytes() == source_bytes


def test_episode_purge_and_state_invalidation_roll_back_together(
    sqlite_store, populated_fixture
):
    before = _counts(sqlite_store)
    with sqlite_store.write_transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_state_invalidation BEFORE UPDATE ON source_states "
            "BEGIN SELECT RAISE(ABORT, 'synthetic invalidation failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="invalidation failure"):
        purge(
            sqlite_store,
            PurgeScope("local", "selected"),
            frozenset({"episodes"}),
        )

    assert _counts(sqlite_store) == before


def test_episode_purge_resets_partial_staging_before_immediate_rebuild(
    sqlite_store, tmp_path
):
    path = tmp_path / "partial-append.jsonl"
    _write_source(path, "first active lifecycle answer")
    source = _source("local", "partial-append", path)
    registry = EnrollmentRegistry((source,))
    reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))
    with path.open("ab") as stream:
        for cycle in (2, 3):
            stream.write(
                json.dumps(
                    {
                        "cycle": cycle,
                        "user_message": f"synthetic lifecycle question {cycle}",
                        "response_text": f"staged lifecycle answer {cycle}",
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
    reconcile_registry(sqlite_store, registry, WorkBudget(1, NOW))
    partial = sqlite_store.member_state(source, source.source_id)
    assert partial["active_generation_id"]
    assert partial["staging_generation_id"]
    assert partial["build_generation_id"] == partial["staging_generation_id"]
    assert partial["build_cursor"]["byte_offset"] > 0
    assert partial["staging_episode_count"] > 0
    staged_count = sqlite_store.staging_episode_count("local", source.source_id)
    assert staged_count > 0

    assert purge(
        sqlite_store,
        PurgeScope("local", source.source_id),
        frozenset({"episodes"}),
    ) == {"episodes": staged_count + 1}

    reset = sqlite_store.member_state(source, source.source_id)
    assert reset["active_generation_id"] == partial["active_generation_id"]
    assert reset["active_generation_integrity"] == "invalid"
    assert reset["freshness"] == "stale"
    for key in (
        "staging_generation_id",
        "staging_episode_count",
        "build_generation_id",
        "build_cursor",
        "build_mode",
        "build_reason",
    ):
        assert reset[key] is None

    response = search_history(
        sqlite_store,
        registry,
        SearchRequest.create(
            "lifecycle",
            ["local"],
            strategy=SQLITE_STRATEGY,
        ),
        WorkBudget(1_000_000, NOW),
    )
    recovered = sqlite_store.member_state(source, source.source_id)
    assert recovered["active_generation_integrity"] == "valid"
    assert recovered["active_generation_id"] != partial["active_generation_id"]
    assert len(sqlite_store.active_episode_refs("local", source.source_id)) == 3
    assert response["total_standing"] == "exact"
    assert response["total_matches"] == 3


def test_episode_purge_resets_staging_only_state_before_initial_rebuild(
    sqlite_store, tmp_path
):
    path = tmp_path / "partial-initial.jsonl"
    with path.open("wb") as stream:
        for cycle in (1, 2):
            stream.write(
                json.dumps(
                    {
                        "cycle": cycle,
                        "user_message": f"initial lifecycle question {cycle}",
                        "response_text": f"initial lifecycle answer {cycle}",
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
    source = _source("local", "partial-initial", path)
    registry = EnrollmentRegistry((source,))
    reconcile_registry(sqlite_store, registry, WorkBudget(1, NOW))
    partial = sqlite_store.member_state(source, source.source_id)
    assert partial.get("active_generation_id") is None
    assert partial["staging_generation_id"]
    assert partial["build_generation_id"] == partial["staging_generation_id"]
    assert partial["build_cursor"]["byte_offset"] > 0
    staged_count = sqlite_store.staging_episode_count("local", source.source_id)
    assert staged_count > 0

    assert purge(
        sqlite_store,
        PurgeScope("local", source.source_id),
        frozenset({"episodes"}),
    ) == {"episodes": staged_count}

    reset = sqlite_store.member_state(source, source.source_id)
    assert reset.get("active_generation_id") is None
    assert reset["active_generation_integrity"] is None
    assert reset["freshness"] == "incomplete"
    assert reset["staging_generation_id"] is None
    assert reset["staging_episode_count"] is None
    assert reset["build_generation_id"] is None
    assert reset["build_cursor"] is None
    reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))
    assert len(sqlite_store.active_episode_refs("local", source.source_id)) == 2


def test_measure_reports_physical_files_and_scoped_query_counts_separately(
    sqlite_store, populated_fixture
):
    measurement = measure(sqlite_store, PurgeScope("local", "selected"))

    assert measurement.provider == "sqlite"
    assert measurement.standing == "available"
    assert measurement.observations["scope"] == "source"
    assert measurement.observations["corpus_id"] == "local"
    assert measurement.observations["source_id"] == "selected"
    assert (
        measurement.observations["database_bytes"]
        == sqlite_store.path.stat().st_size
    )
    assert measurement.observations["database_stat_standing"] == "available"
    assert measurement.observations["wal_stat_standing"] in {"available", "absent"}
    assert measurement.observations["shm_stat_standing"] in {"available", "absent"}
    assert measurement.observations["wal_bytes"] >= 0
    assert measurement.observations["shm_bytes"] >= 0
    assert measurement.observations["query_standing"] == "available"
    assert measurement.observations["episode_documents"] == 1
    assert measurement.observations["episode_fts_rows"] == 1
    assert measurement.observations["source_state_documents"] == 1
    assert measurement.observations["supersession_documents"] == 1
    assert (
        measurement.observations["fts_representation"]
        == "self_contained_duplicate"
    )
    assert "serialized" not in repr(measurement.observations).lower()


def test_measure_reports_absent_file_without_creating_provider(tmp_path):
    store = SQLiteStore(tmp_path / "absent.sqlite3")

    measurement = measure(store, PurgeScope())

    assert measurement.standing == "unavailable"
    assert measurement.observations["scope"] == "global"
    assert measurement.observations["corpus_id"] is None
    assert measurement.observations["source_id"] is None
    assert measurement.observations["database_bytes"] == 0
    assert measurement.observations["wal_bytes"] == 0
    assert measurement.observations["shm_bytes"] == 0
    assert measurement.observations["database_stat_standing"] == "absent"
    assert measurement.observations["wal_stat_standing"] == "absent"
    assert measurement.observations["shm_stat_standing"] == "absent"
    assert measurement.observations["query_standing"] == "unavailable"
    assert measurement.observations["episode_documents"] is None
    assert measurement.observations["episode_fts_rows"] is None
    assert measurement.observations["source_state_documents"] is None
    assert measurement.observations["supersession_documents"] is None
    assert not store.path.exists()


def test_full_removal_handles_database_wal_shm_and_preserves_sources(
    sqlite_store, populated_fixture
):
    source_bytes = {
        source.locator: source.locator.read_bytes()
        for source in populated_fixture["sources"]
    }
    config = sqlite_store.path.parent / "enrollment.yaml"
    config_bytes = b"sources:\n  - corpus_id: local\n    source_id: selected\n"
    config.write_bytes(config_bytes)
    held_connection = sqlite_store.connect()
    held_connection.execute("BEGIN IMMEDIATE")
    held_connection.execute(
        "UPDATE provider_meta SET value = value WHERE key = 'schema_version'"
    )
    held_connection.commit()
    candidates = {
        sqlite_store.path.name,
        f"{sqlite_store.path.name}-wal",
        f"{sqlite_store.path.name}-shm",
    }
    assert candidates <= {path.name for path in sqlite_store.path.parent.iterdir()}
    try:
        report = remove_provider_file(sqlite_store)
    finally:
        held_connection.close()

    assert set(report["removed_paths"]) == candidates
    assert report["residual_paths"] == []
    assert report["declared_losses"] == [
        "retained supersession observations",
        "non-reproducible evaluation state",
    ]
    assert report["retained"] == ["enrollment configuration", "source locators"]
    assert not any((sqlite_store.path.parent / name).exists() for name in candidates)
    assert all(path.read_bytes() == content for path, content in source_bytes.items())
    assert config.read_bytes() == config_bytes


def test_full_removal_is_idempotent_when_provider_files_are_absent(tmp_path):
    store = SQLiteStore(tmp_path / "absent.sqlite3")

    first = remove_provider_file(store)
    second = remove_provider_file(store)

    assert first["removed_paths"] == []
    assert first["residual_paths"] == []
    assert second == first
    assert not store.path.exists()


def test_full_removal_reports_residual_path_when_unlink_fails(
    sqlite_store, monkeypatch
):
    original_unlink = Path.unlink

    def fail_database(path, *args, **kwargs):
        if path == sqlite_store.path:
            raise PermissionError("synthetic removal denial")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_database)

    report = remove_provider_file(sqlite_store)

    assert sqlite_store.path.name not in report["removed_paths"]
    assert report["residual_paths"] == [sqlite_store.path.name]
    assert sqlite_store.path.exists()


@pytest.mark.parametrize("target_exists", [True, False])
def test_symlink_provider_measurement_and_removal_refuse_to_follow_or_unlink(
    tmp_path, target_exists
):
    target = tmp_path / "unexpected-target.sqlite3"
    if target_exists:
        target.write_bytes(b"unexpected target bytes")
    link = tmp_path / "configured.sqlite3"
    link.symlink_to(target)
    companions = (Path(f"{link}-wal"), Path(f"{link}-shm"))
    for companion in companions:
        companion.write_bytes(b"unexpected companion bytes")
    store = SQLiteStore(link)
    target_before = target.read_bytes() if target_exists else None

    with pytest.raises(ProviderUnsupported, match="symlink"):
        measure(store, PurgeScope())

    report = remove_provider_file(store)
    assert report["removed_paths"] == []
    assert report["residual_paths"] == [
        link.name,
        *(companion.name for companion in companions),
    ]
    assert report["residual_reasons"] == {
        link.name: "configured SQLite database path is a symlink",
        **{
            companion.name: (
                "not removed because configured SQLite database path is a symlink"
            )
            for companion in companions
        },
    }
    assert link.is_symlink()
    assert all(
        companion.read_bytes() == b"unexpected companion bytes"
        for companion in companions
    )
    assert target.exists() is target_exists
    if target_exists:
        assert target.read_bytes() == target_before


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("target_exists", [True, False])
def test_companion_symlink_blocks_measurement_and_removal_without_target_access(
    tmp_path, suffix, target_exists
):
    store = SQLiteStore(tmp_path / "configured.sqlite3")
    store.ensure()
    database_bytes = store.path.read_bytes()
    target = tmp_path / f"unexpected-target{suffix}"
    if target_exists:
        target.write_bytes(b"unexpected target bytes")
    companion = Path(f"{store.path}{suffix}")
    companion.symlink_to(target)
    target_before = target.read_bytes() if target_exists else None

    with pytest.raises(ProviderUnsupported, match="symlink"):
        measure(store, PurgeScope())

    report = remove_provider_file(store)
    assert report["removed_paths"] == []
    assert store.path.name in report["residual_paths"]
    assert companion.name in report["residual_paths"]
    assert "symlink" in report["residual_reasons"][companion.name]
    assert companion.is_symlink()
    assert store.path.read_bytes() == database_bytes
    assert target.exists() is target_exists
    if target_exists:
        assert target.read_bytes() == target_before
