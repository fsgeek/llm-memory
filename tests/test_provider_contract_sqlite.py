from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from llm_memory.contract import ContractError, SearchRequest
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.provider import PurgeScope
from llm_memory.reconcile import WorkBudget
from llm_memory.sqlite_history import SQLITE_STRATEGY
from llm_memory.sqlite_provider import SQLiteProvider

from provider_contract import assert_portable_provider_contract


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def test_sqlite_provider_satisfies_portable_contract(tmp_path, synthetic_source):
    provider = SQLiteProvider(tmp_path / "portable.sqlite3")
    rewritten_bytes = assert_portable_provider_contract(
        provider,
        synthetic_source,
        strategy=SQLITE_STRATEGY,
        foreign_strategy="lexical_bm25_text_en_v1",
    )

    held_connection = provider.store.connect()
    held_connection.execute("BEGIN IMMEDIATE")
    held_connection.execute(
        "UPDATE provider_meta SET value = value WHERE key = 'schema_version'"
    )
    held_connection.commit()
    candidates = {path.name for path in provider.store.file_paths()}
    assert candidates <= {
        path.name for path in provider.store.path.parent.iterdir()
    }
    try:
        removal = provider.remove_all()
    finally:
        held_connection.close()
    assert set(removal["removed_paths"]) == candidates
    assert removal["residual_paths"] == []
    assert removal["residual_reasons"] == {}
    assert removal["declared_losses"] == [
        "retained supersession observations",
        "non-reproducible evaluation state",
    ]
    assert removal["retained"] == [
        "enrollment configuration",
        "source locators",
    ]
    assert not any(path.exists() for path in provider.store.file_paths())
    unavailable = provider.measure(PurgeScope())
    assert unavailable.standing == "unavailable"
    assert unavailable.observations["query_standing"] == "unavailable"
    assert all(
        unavailable.observations[key] is None
        for key in (
            "episode_documents",
            "episode_fts_rows",
            "source_state_documents",
            "supersession_documents",
        )
    )
    assert synthetic_source.path.read_bytes() == rewritten_bytes


def test_sqlite_malformed_nul_query_fails_as_contract_error(tmp_path):
    source_path = tmp_path / "source.jsonl"
    source_path.write_text(
        json.dumps(
            {
                "cycle": 1,
                "user_message": "portable decision",
                "response_text": "retain exact source authority",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry = EnrollmentRegistry(
        (
            SourceEnrollment(
                corpus_id="local",
                source_id="source-a",
                adapter="taste_open_jsonl",
                boundary_version=1,
                canonicalization_version=1,
                locator=source_path,
                enabled=True,
                full_validation_max_age_seconds=3600,
            ),
        )
    )
    provider = SQLiteProvider(tmp_path / "episodes.sqlite3")
    provider.ensure()

    with pytest.raises(ContractError, match="query"):
        provider.search(
            registry,
            SearchRequest.create(
                "decision\0hidden",
                ["local"],
                strategy=SQLITE_STRATEGY,
            ),
            WorkBudget(1_000_000, NOW),
        )
