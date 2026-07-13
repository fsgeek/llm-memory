from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

import llm_memory.history as history_module
from llm_memory.adapters import get_adapter
from llm_memory.contract import ContractError
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    SOURCE_STATES,
    SUPERSESSIONS,
    ensure_contract_index,
)
from llm_memory.db import get_database
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.history import open_episode
from llm_memory.reconcile import WorkBudget, reconcile_registry


NOW = datetime(2026, 7, 12, 18, 30, tzinfo=UTC)
_CONTENT_KEYS = {
    "timestamp",
    "model",
    "user_message",
    "response",
    "state",
    "activity_log",
    "adapter_fields",
    "provenance",
    "snippet",
}


@pytest.fixture
def opening_storage():
    db = get_database()
    ensure_contract_index(db)
    prefix = f"open-test-{uuid4().hex}"
    try:
        yield db, prefix
    finally:
        for collection_name in (CONTRACT_EPISODES, SOURCE_STATES, SUPERSESSIONS):
            db.aql.execute(
                """
                FOR doc IN @@collection
                    FILTER STARTS_WITH(doc.corpus_id, @prefix)
                    REMOVE doc IN @@collection
                """,
                bind_vars={"@collection": collection_name, "prefix": prefix},
            )


def enrollment(corpus_id: str, path: Path, *, canonicalization_version: int = 1):
    return SourceEnrollment(
        corpus_id=corpus_id,
        source_id="taste",
        adapter="taste_open_jsonl",
        boundary_version=1,
        canonicalization_version=canonicalization_version,
        locator=path,
        enabled=True,
        full_validation_max_age_seconds=3600,
    )


def taste(cycle: int, question: str, response: str) -> dict:
    return {
        "cycle": cycle,
        "timestamp": "2026-07-12T18:30:00Z",
        "model": "test-model",
        "user_message": question,
        "response_text": response,
        "state": {"topic": "opening", "_activity_log": ["observed"]},
        "experiment_label": "stage-1",
    }


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def first_record(source: SourceEnrollment):
    adapter = get_adapter(source.adapter)
    return adapter.scan(source, adapter.members(source)[0]).episodes[0]


def assert_standing(response: dict, episode_ref: str, standing: str) -> None:
    assert response["contract_version"] == 1
    assert response["episode_ref"] == episode_ref
    assert response["standing"] == standing
    assert sum(key == "standing" for key in response) == 1


def assert_no_content(response: dict) -> None:
    assert _CONTENT_KEYS.isdisjoint(response)


class UnavailableDatabase:
    def __getattr__(self, name):
        raise OSError(f"database is unavailable: {name}")


def test_available_opening_is_recomputed_from_source_without_arango(tmp_path):
    path = tmp_path / "available.jsonl"
    write_jsonl(path, [taste(7, "What was decided?", "Use source authority.")])
    source = enrollment("available-corpus", path)
    record = first_record(source)
    episode_ref = record.identity.episode_ref

    response = open_episode(
        UnavailableDatabase(),
        EnrollmentRegistry((source,)),
        episode_ref,
        [source.corpus_id],
    )

    assert response == {
        "contract_version": 1,
        "episode_ref": episode_ref,
        "standing": "available",
        **record.body.as_dict(),
        "provenance": {
            "corpus_id": source.corpus_id,
            "source_id": source.source_id,
            "adapter": source.adapter,
            "implementation_version": "1",
            "canonicalization_version": 1,
            "boundary_version": 1,
            "native_event_id": "7",
            "source_position": record.source_position,
            "content_digest": record.identity.body_digest,
        },
    }


@pytest.mark.parametrize("active_corpora", [[], ["scoped-corpus", "scoped-corpus"]])
def test_opening_rejects_inactive_or_ambiguous_corpus_scope(tmp_path, active_corpora):
    path = tmp_path / "scope.jsonl"
    write_jsonl(path, [taste(1, "question", "answer")])
    source = enrollment("scoped-corpus", path)
    episode_ref = first_record(source).identity.episode_ref

    with pytest.raises(ContractError, match="active corpus scope"):
        open_episode(
            UnavailableDatabase(),
            EnrollmentRegistry((source,)),
            episode_ref,
            active_corpora,
        )


def test_opening_reports_missing_when_the_source_event_is_absent(opening_storage, tmp_path):
    db, corpus_id = opening_storage
    path = tmp_path / "missing-event.jsonl"
    write_jsonl(path, [taste(1, "old", "answer")])
    source = enrollment(corpus_id, path)
    episode_ref = first_record(source).identity.episode_ref
    write_jsonl(path, [taste(2, "new", "answer")])

    response = open_episode(db, EnrollmentRegistry((source,)), episode_ref, [corpus_id])

    assert_standing(response, episode_ref, "missing")
    assert_no_content(response)


def test_opening_reports_malformed_source_without_partial_content(opening_storage, tmp_path):
    db, corpus_id = opening_storage
    path = tmp_path / "malformed.jsonl"
    write_jsonl(path, [taste(1, "question", "answer")])
    source = enrollment(corpus_id, path)
    episode_ref = first_record(source).identity.episode_ref
    path.write_bytes(b"{not-json}\n")

    response = open_episode(db, EnrollmentRegistry((source,)), episode_ref, [corpus_id])

    assert_standing(response, episode_ref, "malformed_source")
    assert_no_content(response)


def test_opening_reports_source_unavailable_without_content(opening_storage, tmp_path):
    db, corpus_id = opening_storage
    path = tmp_path / "unavailable.jsonl"
    write_jsonl(path, [taste(1, "question", "answer")])
    source = enrollment(corpus_id, path)
    episode_ref = first_record(source).identity.episode_ref
    path.unlink()
    path.mkdir()

    response = open_episode(db, EnrollmentRegistry((source,)), episode_ref, [corpus_id])

    assert_standing(response, episode_ref, "source_unavailable")
    assert_no_content(response)


def test_opening_reports_content_mismatch_after_rewrite(opening_storage, tmp_path):
    db, corpus_id = opening_storage
    path = tmp_path / "rewrite.jsonl"
    write_jsonl(path, [taste(1, "question", "old answer")])
    source = enrollment(corpus_id, path)
    episode_ref = first_record(source).identity.episode_ref
    write_jsonl(path, [taste(1, "question", "rewritten answer")])

    response = open_episode(db, EnrollmentRegistry((source,)), episode_ref, [corpus_id])

    assert_standing(response, episode_ref, "content_mismatch")
    assert_no_content(response)


def test_opening_reports_verified_supersession_with_replacement_ref(
    opening_storage, tmp_path, enable_semantic_version
):
    db, corpus_id = opening_storage
    path = tmp_path / "superseded.jsonl"
    write_jsonl(path, [taste(1, "question", "answer")])
    original = enrollment(corpus_id, path)
    reconcile_registry(db, EnrollmentRegistry((original,)), WorkBudget(1_000_000, NOW))
    old_ref = first_record(original).identity.episode_ref
    enable_semantic_version("taste_open_jsonl", canonicalization=2)
    replacement = replace(original, canonicalization_version=2)
    reconcile_registry(
        db,
        EnrollmentRegistry((replacement,)),
        WorkBudget(1_000_000, NOW),
    )
    new_ref = first_record(replacement).identity.episode_ref

    response = open_episode(
        db, EnrollmentRegistry((replacement,)), old_ref, [corpus_id]
    )

    assert response == {
        "contract_version": 1,
        "episode_ref": old_ref,
        "standing": "superseded",
        "replacement_ref": new_ref,
    }


@pytest.mark.parametrize(
    ("replacement_cycle", "expected_standing"),
    [(1, "content_mismatch"), (2, "missing")],
)
def test_purged_supersession_observation_degrades_honestly(
    opening_storage, tmp_path, replacement_cycle, expected_standing
):
    db, corpus_id = opening_storage
    path = tmp_path / f"purged-{replacement_cycle}.jsonl"
    write_jsonl(path, [taste(1, "question", "old answer")])
    source = enrollment(corpus_id, path)
    old_ref = first_record(source).identity.episode_ref
    write_jsonl(path, [taste(replacement_cycle, "question", "new answer")])
    new_ref = first_record(source).identity.episode_ref
    db.collection(SUPERSESSIONS).insert(
        {
            "_key": uuid4().hex,
            "corpus_id": corpus_id,
            "source_id": source.source_id,
            "member_id": "purged",
            "event_token": str(replacement_cycle),
            "old_ref": old_ref,
            "new_ref": new_ref,
            "reason": "source_content",
            "detected_at": NOW.isoformat(),
        }
    )
    db.aql.execute(
        "FOR doc IN @@supersessions FILTER doc.old_ref == @old REMOVE doc IN @@supersessions",
        bind_vars={"@supersessions": SUPERSESSIONS, "old": old_ref},
    )

    response = open_episode(db, EnrollmentRegistry((source,)), old_ref, [corpus_id])

    assert_standing(response, old_ref, expected_standing)
    assert_no_content(response)


def test_supersession_lookup_failure_is_not_silently_reported_as_content_mismatch(
    tmp_path,
):
    path = tmp_path / "lookup-failure.jsonl"
    write_jsonl(path, [taste(1, "question", "old answer")])
    source = enrollment("lookup-failure", path)
    old_ref = first_record(source).identity.episode_ref
    write_jsonl(path, [taste(1, "question", "new answer")])

    class FailingAQL:
        def execute(self, *args, **kwargs):
            raise RuntimeError("supersession store unavailable")

    class FailingDB:
        aql = FailingAQL()

    with pytest.raises(RuntimeError, match="supersession store unavailable"):
        open_episode(
            FailingDB(), EnrollmentRegistry((source,)), old_ref, [source.corpus_id]
        )


def test_opening_reports_unsupported_adapter_without_content(
    opening_storage, tmp_path, monkeypatch
):
    db, corpus_id = opening_storage
    path = tmp_path / "unsupported.jsonl"
    write_jsonl(path, [taste(1, "question", "answer")])
    source = enrollment(corpus_id, path)
    episode_ref = first_record(source).identity.episode_ref

    def unsupported(_name):
        raise ContractError("unsupported adapter")

    monkeypatch.setattr(history_module, "get_adapter", unsupported)
    response = open_episode(db, EnrollmentRegistry((source,)), episode_ref, [corpus_id])

    assert_standing(response, episode_ref, "unsupported_adapter")
    assert_no_content(response)


def test_retained_arango_episode_cannot_replace_missing_authoritative_source(
    opening_storage, tmp_path
):
    db, corpus_id = opening_storage
    path = tmp_path / "cached.jsonl"
    renamed = tmp_path / "cached.jsonl.removed"
    write_jsonl(path, [taste(1, "source question", "source answer")])
    source = enrollment(corpus_id, path)
    record = first_record(source)
    episode_ref = record.identity.episode_ref
    reconcile_registry(db, EnrollmentRegistry((source,)), WorkBudget(1_000_000, NOW))
    path.rename(renamed)
    retained = list(
        db.aql.execute(
            "FOR doc IN @@episodes FILTER doc.episode_ref == @ref RETURN doc",
            bind_vars={"@episodes": CONTRACT_EPISODES, "ref": episode_ref},
        )
    )
    assert retained and retained[0]["response"] == "source answer"

    response = open_episode(db, EnrollmentRegistry((source,)), episode_ref, [corpus_id])

    assert_standing(response, episode_ref, "source_unavailable")
    assert_no_content(response)
