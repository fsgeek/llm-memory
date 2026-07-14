from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from llm_memory.contract import ContractError, SearchRequest
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.reconcile import WorkBudget
from llm_memory.sqlite_history import SQLITE_STRATEGY
from llm_memory.sqlite_provider import SQLiteProvider

from provider_contract import assert_portable_provider_contract


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def test_sqlite_provider_satisfies_portable_contract(tmp_path, synthetic_source):
    assert_portable_provider_contract(
        SQLiteProvider(tmp_path / "portable.sqlite3"),
        synthetic_source,
        strategy=SQLITE_STRATEGY,
        foreign_strategy="lexical_bm25_text_en_v1",
    )


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
