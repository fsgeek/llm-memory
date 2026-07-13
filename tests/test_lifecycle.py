from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

import llm_memory.lifecycle as lifecycle_module
from llm_memory.adapters import get_adapter
from llm_memory.contract import ContractError, SearchRequest
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    SOURCE_STATES,
    SUPERSESSIONS,
    active_states,
    ensure_contract_index,
)
from llm_memory.db import get_database
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment, load_registry
from llm_memory.history import open_episode, search_history
from llm_memory.index import EPISODES as LEGACY_EPISODES
from llm_memory.lifecycle import disable_source, purge_derived, unenroll_source
from llm_memory.reconcile import WorkBudget, reconcile_registry


NOW = datetime(2026, 7, 12, 18, 30, tzinfo=UTC)
DERIVED_COLLECTIONS = {
    "episodes": CONTRACT_EPISODES,
    "reconciliation": SOURCE_STATES,
    "supersessions": SUPERSESSIONS,
}


@pytest.fixture
def lifecycle_storage():
    db = get_database()
    ensure_contract_index(db)
    if not db.has_collection(LEGACY_EPISODES):
        db.create_collection(LEGACY_EPISODES)
    prefix = f"lifecycle-test-{uuid4().hex}"
    try:
        yield db, prefix
    finally:
        for collection_name in (*DERIVED_COLLECTIONS.values(), LEGACY_EPISODES):
            db.aql.execute(
                """
                FOR doc IN @@collection
                    FILTER STARTS_WITH(doc.corpus_id, @prefix)
                    REMOVE doc IN @@collection
                """,
                bind_vars={"@collection": collection_name, "prefix": prefix},
            )


def taste(cycle: int, response: str) -> dict:
    return {
        "cycle": cycle,
        "timestamp": "2026-07-12T18:30:00Z",
        "user_message": "heliotrope lifecycle question",
        "response_text": response,
    }


def write_jsonl(path: Path, records: list[dict]) -> bytes:
    data = b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n"
        for record in records
    )
    path.write_bytes(data)
    return data


def source_mapping(
    corpus_id: str,
    source_id: str,
    locator: Path,
    *,
    enabled: bool = True,
    canonicalization_version: int = 1,
) -> dict:
    return {
        "corpus_id": corpus_id,
        "source_id": source_id,
        "adapter": "taste_open_jsonl",
        "boundary_version": 1,
        "canonicalization_version": canonicalization_version,
        "locator": str(locator),
        "enabled": enabled,
        "full_validation_max_age_seconds": 3600,
    }


def write_config(path: Path, *sources: dict) -> bytes:
    data = yaml.safe_dump(
        {"contract_version": 1, "sources": list(sources)},
        sort_keys=False,
    ).encode()
    path.write_bytes(data)
    return data


def run(db, registry: EnrollmentRegistry, *, now: datetime = NOW):
    return reconcile_registry(db, registry, WorkBudget(1_000_000, now))


def records(db, collection_name: str, corpus_id: str) -> list[dict]:
    return list(
        db.aql.execute(
            """
            FOR doc IN @@collection
                FILTER doc.corpus_id == @corpus_id
                SORT doc.source_id, doc._key
                RETURN doc
            """,
            bind_vars={"@collection": collection_name, "corpus_id": corpus_id},
        )
    )


def first_ref(source: SourceEnrollment) -> str:
    adapter = get_adapter(source.adapter)
    member = adapter.members(source)[0]
    return adapter.scan(source, member).episodes[0].identity.episode_ref


def test_disable_is_atomic_preserves_declarations_and_source_bytes_and_revokes_search(
    lifecycle_storage, tmp_path, monkeypatch
):
    db, corpus_id = lifecycle_storage
    source_path = tmp_path / "source.jsonl"
    source_bytes = write_jsonl(source_path, [taste(1, "heliotrope lifecycle answer")])
    sibling_path = tmp_path / "sibling.jsonl"
    sibling_bytes = write_jsonl(sibling_path, [taste(2, "sibling answer")])
    declaration = source_mapping(corpus_id, "taste", source_path)
    sibling = source_mapping(corpus_id + "-sibling", "other", sibling_path)
    config_path = tmp_path / "sources.yaml"
    write_config(config_path, declaration, sibling)
    run(db, load_registry(config_path))
    derived_before = records(db, CONTRACT_EPISODES, corpus_id)
    replace_calls = []
    real_replace = os.replace

    def tracked_replace(source, destination):
        replace_calls.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(lifecycle_module.os, "replace", tracked_replace)

    report = disable_source(config_path, corpus_id, "taste")

    assert report == {
        "operation": "disable",
        "corpus_id": corpus_id,
        "source_id": "taste",
        "changed": True,
    }
    assert len(replace_calls) == 1
    temporary, destination = replace_calls[0]
    assert temporary.parent == config_path.parent
    assert destination == config_path
    registry = load_registry(config_path)
    assert registry.sources[0].enabled is False
    assert registry.sources[1] == SourceEnrollment(
        corpus_id=sibling["corpus_id"],
        source_id=sibling["source_id"],
        adapter=sibling["adapter"],
        boundary_version=sibling["boundary_version"],
        canonicalization_version=sibling["canonicalization_version"],
        locator=sibling_path,
        enabled=True,
        full_validation_max_age_seconds=sibling["full_validation_max_age_seconds"],
    )
    assert source_path.read_bytes() == source_bytes
    assert sibling_path.read_bytes() == sibling_bytes
    assert records(db, CONTRACT_EPISODES, corpus_id) == derived_before
    with pytest.raises(ContractError, match="disabled corpus"):
        search_history(
            db,
            registry,
            SearchRequest.create("heliotrope", [corpus_id]),
            WorkBudget(1_000_000, NOW),
        )


def test_unenroll_removes_only_declaration_and_authority_but_retains_derived_data(
    lifecycle_storage, tmp_path
):
    db, corpus_id = lifecycle_storage
    source_path = tmp_path / "source.jsonl"
    source_bytes = write_jsonl(source_path, [taste(1, "retained answer")])
    sibling_path = tmp_path / "sibling.jsonl"
    write_jsonl(sibling_path, [taste(2, "sibling answer")])
    declaration = source_mapping(corpus_id, "taste", source_path)
    sibling = source_mapping(corpus_id + "-sibling", "other", sibling_path)
    config_path = tmp_path / "sources.yaml"
    write_config(config_path, declaration, sibling)
    registry = load_registry(config_path)
    run(db, registry)
    episode_ref = first_ref(registry.sources[0])
    derived_before = {
        name: records(db, collection, corpus_id)
        for name, collection in DERIVED_COLLECTIONS.items()
    }

    report = unenroll_source(config_path, corpus_id, "taste")

    assert report == {
        "operation": "unenroll",
        "corpus_id": corpus_id,
        "source_id": "taste",
        "changed": True,
    }
    updated = load_registry(config_path)
    assert len(updated.sources) == 1
    assert updated.sources[0].corpus_id == sibling["corpus_id"]
    assert source_path.read_bytes() == source_bytes
    assert {
        name: records(db, collection, corpus_id)
        for name, collection in DERIVED_COLLECTIONS.items()
    } == derived_before
    with pytest.raises(ContractError, match="unknown corpus"):
        search_history(
            db,
            updated,
            SearchRequest.create("heliotrope", [corpus_id]),
            WorkBudget(1_000_000, NOW),
        )
    with pytest.raises(ContractError, match="not uniquely enrolled and enabled"):
        open_episode(db, updated, episode_ref, [corpus_id])


@pytest.mark.parametrize("operation", [disable_source, unenroll_source])
def test_failed_configuration_parse_preserves_malformed_yaml_byte_for_byte(
    tmp_path, operation
):
    config_path = tmp_path / "malformed.yaml"
    malformed = b"contract_version: 1\nsources:\n  - corpus_id: [unterminated\n"
    config_path.write_bytes(malformed)

    with pytest.raises(yaml.YAMLError):
        operation(config_path, "corpus", "source")

    assert config_path.read_bytes() == malformed
    assert list(tmp_path.iterdir()) == [config_path]


def test_purge_deletes_only_requested_classes_scope_and_reports_counts(
    lifecycle_storage
):
    db, corpus_id = lifecycle_storage
    other_corpus = corpus_id + "-other"
    for name, collection_name in DERIVED_COLLECTIONS.items():
        for source_id in ("selected", "retained"):
            db.collection(collection_name).insert(
                {
                    "_key": uuid4().hex,
                    "corpus_id": corpus_id,
                    "source_id": source_id,
                    "kind": name,
                }
            )
        db.collection(collection_name).insert(
            {
                "_key": uuid4().hex,
                "corpus_id": other_corpus,
                "source_id": "selected",
                "kind": name,
            }
        )
    legacy_key = uuid4().hex
    db.collection(LEGACY_EPISODES).insert(
        {
            "_key": legacy_key,
            "corpus_id": corpus_id,
            "source_id": "selected",
        }
    )

    report = purge_derived(
        db,
        corpus_id,
        "selected",
        classes=frozenset({"episodes", "supersessions"}),
    )

    assert report == {"episodes": 1, "supersessions": 1}
    assert [
        (doc["source_id"], doc["kind"])
        for doc in records(db, CONTRACT_EPISODES, corpus_id)
    ] == [("retained", "episodes")]
    assert sorted(
        doc["source_id"] for doc in records(db, SOURCE_STATES, corpus_id)
    ) == [
        "retained",
        "selected",
    ]
    assert [
        (doc["source_id"], doc["kind"])
        for doc in records(db, SUPERSESSIONS, corpus_id)
    ] == [("retained", "supersessions")]
    assert len(records(db, CONTRACT_EPISODES, other_corpus)) == 1
    assert len(records(db, SOURCE_STATES, other_corpus)) == 1
    assert len(records(db, SUPERSESSIONS, other_corpus)) == 1
    assert db.collection(LEGACY_EPISODES).get(legacy_key) is not None

    assert purge_derived(
        db,
        corpus_id,
        classes=frozenset({"reconciliation"}),
    ) == {"reconciliation": 2}
    assert records(db, SOURCE_STATES, corpus_id) == []


@pytest.mark.parametrize(
    "classes",
    [frozenset(), frozenset({"legacy"}), {"episodes"}, "episodes"],
)
def test_invalid_purge_classes_fail_before_deletion(lifecycle_storage, classes):
    db, corpus_id = lifecycle_storage
    key = uuid4().hex
    db.collection(CONTRACT_EPISODES).insert(
        {
            "_key": key,
            "corpus_id": corpus_id,
            "source_id": "source",
        }
    )

    with pytest.raises(ValueError, match="classes"):
        purge_derived(db, corpus_id, classes=classes)

    assert db.collection(CONTRACT_EPISODES).get(key) is not None


def test_purging_supersessions_degrades_old_reference_standing_honestly(
    lifecycle_storage, tmp_path, enable_semantic_version
):
    db, corpus_id = lifecycle_storage
    source_path = tmp_path / "supersession.jsonl"
    write_jsonl(source_path, [taste(1, "answer")])
    original = SourceEnrollment(
        corpus_id=corpus_id,
        source_id="taste",
        adapter="taste_open_jsonl",
        boundary_version=1,
        canonicalization_version=1,
        locator=source_path,
        enabled=True,
        full_validation_max_age_seconds=3600,
    )
    run(db, EnrollmentRegistry((original,)))
    old_ref = first_ref(original)
    enable_semantic_version("taste_open_jsonl", canonicalization=2)
    replacement = replace(original, canonicalization_version=2)
    run(db, EnrollmentRegistry((replacement,)))
    assert open_episode(
        db, EnrollmentRegistry((replacement,)), old_ref, [corpus_id]
    )["standing"] == "superseded"

    report = purge_derived(
        db,
        corpus_id,
        "taste",
        classes=frozenset({"supersessions"}),
    )

    assert report == {"supersessions": 1}
    response = open_episode(
        db, EnrollmentRegistry((replacement,)), old_ref, [corpus_id]
    )
    assert response == {
        "contract_version": 1,
        "episode_ref": old_ref,
        "standing": "content_mismatch",
    }


def test_reenrollment_validates_retained_state_then_rebuilds_changed_source(
    lifecycle_storage, tmp_path
):
    db, corpus_id = lifecycle_storage
    source_path = tmp_path / "retained.jsonl"
    write_jsonl(source_path, [taste(1, "old-value")])
    declaration = source_mapping(corpus_id, "taste", source_path)
    config_path = tmp_path / "sources.yaml"
    enrolled_config = write_config(config_path, declaration)
    original = load_registry(config_path)
    run(db, original)
    original_generation = active_states(db, (corpus_id,))[0]["active_generation_id"]

    unenroll_source(config_path, corpus_id, "taste")
    write_jsonl(source_path, [taste(1, "new-value")])
    config_path.write_bytes(enrolled_config)
    reenrolled = load_registry(config_path)

    validation = run(db, reenrolled)

    member = validation.corpus_standing[0]["sources"][0]["members"][0]
    assert member["freshness"] == "stale"
    assert active_states(db, (corpus_id,))[0]["active_generation_id"] == original_generation

    rebuilt = run(db, reenrolled)
    member = rebuilt.corpus_standing[0]["sources"][0]["members"][0]
    assert member["freshness"] == "current"
    assert active_states(db, (corpus_id,))[0]["active_generation_id"] != original_generation


def test_reenrollment_with_new_semantic_version_rebuilds_retained_state(
    lifecycle_storage, tmp_path, enable_semantic_version
):
    db, corpus_id = lifecycle_storage
    source_path = tmp_path / "semantic.jsonl"
    source_bytes = write_jsonl(source_path, [taste(1, "stable source")])
    config_path = tmp_path / "sources.yaml"
    original_declaration = source_mapping(corpus_id, "taste", source_path)
    write_config(config_path, original_declaration)
    original = load_registry(config_path)
    run(db, original)
    original_generation = active_states(db, (corpus_id,))[0]["active_generation_id"]
    original_ref = first_ref(original.sources[0])

    unenroll_source(config_path, corpus_id, "taste")
    enable_semantic_version("taste_open_jsonl", canonicalization=2)
    write_config(
        config_path,
        source_mapping(
            corpus_id,
            "taste",
            source_path,
            canonicalization_version=2,
        ),
    )

    reenrolled = load_registry(config_path)
    report = run(db, reenrolled)

    member = report.corpus_standing[0]["sources"][0]["members"][0]
    assert member["freshness"] == "current"
    assert active_states(db, (corpus_id,))[0]["active_generation_id"] != original_generation
    assert first_ref(reenrolled.sources[0]) != original_ref
    assert source_path.read_bytes() == source_bytes
