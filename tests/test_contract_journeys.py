from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from eval.contract_journeys import main
from llm_memory.adapters import get_adapter
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    SOURCE_STATES,
    SUPERSESSIONS,
    ensure_contract_index,
)
from llm_memory.db import get_database
from llm_memory.enrollment import load_registry


FORBIDDEN_SOURCE = "FORBIDDEN_SOURCE_LINE_71c6"
FORBIDDEN_RESPONSE = "FORBIDDEN_EPISODE_CONTENT_f083"
FORBIDDEN_LOCATOR = "FORBIDDEN_ABSOLUTE_LOCATOR_25ad"
FORBIDDEN_CREDENTIAL = "FORBIDDEN_DB_CREDENTIAL_c622"


def _write_fixture(path: Path) -> None:
    record = {
        "cycle": 17,
        "timestamp": "2026-07-12T18:30:00Z",
        "model": "evaluation-model",
        "user_message": f"bounded evaluation {FORBIDDEN_SOURCE}",
        "response_text": FORBIDDEN_RESPONSE,
        "state": {"credential": FORBIDDEN_CREDENTIAL},
        "experiment_label": "stage-1-evaluation",
    }
    path.write_text(
        json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def _write_config(path: Path, source: Path, corpus_id: str, source_id: str) -> None:
    config = {
        "contract_version": 1,
        "sources": [
            {
                "corpus_id": corpus_id,
                "source_id": source_id,
                "adapter": "taste_open_jsonl",
                "boundary_version": 1,
                "canonicalization_version": 1,
                "locator": str(source),
                "enabled": True,
                "full_validation_max_age_seconds": 3600,
            }
        ],
    }
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _purge_prefix(db, prefix: str) -> None:
    for collection_name in (CONTRACT_EPISODES, SOURCE_STATES, SUPERSESSIONS):
        db.aql.execute(
            """
            FOR doc IN @@collection
                FILTER STARTS_WITH(doc.corpus_id, @prefix)
                REMOVE doc IN @@collection
            """,
            bind_vars={"@collection": collection_name, "prefix": prefix},
        )


def _derived_count(db, corpus_id: str) -> int:
    return sum(
        list(
            db.aql.execute(
                """
                FOR doc IN @@collection
                    FILTER doc.corpus_id == @corpus_id
                    COLLECT WITH COUNT INTO count
                    RETURN count
                """,
                bind_vars={"@collection": name, "corpus_id": corpus_id},
            )
        )[0]
        for name in (CONTRACT_EPISODES, SOURCE_STATES, SUPERSESSIONS)
    )


def test_cli_emits_atomic_non_content_journey_with_digests_and_counts(tmp_path):
    db = get_database()
    ensure_contract_index(db)
    prefix = f"test-contract-journey-{uuid4().hex}"
    corpus_id = f"{prefix}-{FORBIDDEN_CREDENTIAL}"
    source_id = f"source-{FORBIDDEN_SOURCE}"
    source = tmp_path / f"{FORBIDDEN_LOCATOR}.jsonl"
    config = tmp_path / "sources.yaml"
    output = tmp_path / "journey.json"
    _write_fixture(source)
    _write_config(config, source, corpus_id, source_id)

    registry = load_registry(config)
    enrollment = registry.sources[0]
    adapter = get_adapter(enrollment.adapter)
    episode_ref = adapter.scan(enrollment, adapter.members(enrollment)[0]).episodes[
        0
    ].identity.episode_ref

    try:
        assert main(
            [
                "--config",
                str(config),
                "--query",
                "bounded evaluation",
                "--limit",
                "1",
                "--expected-ref",
                episode_ref,
                "--output",
                str(output),
                "--purge-test-corpus",
            ]
        ) == 0
        report = json.loads(output.read_text(encoding="utf-8"))
        serialized = json.dumps(report, sort_keys=True)

        for marker in (
            FORBIDDEN_SOURCE,
            FORBIDDEN_RESPONSE,
            FORBIDDEN_LOCATOR,
            FORBIDDEN_CREDENTIAL,
            str(source),
            episode_ref,
        ):
            assert marker not in serialized

        assert report["contract_version"] == 1
        assert report["query"]["digest"] == hashlib.sha256(
            b"bounded evaluation"
        ).hexdigest()
        assert report["query"]["limit"] == 1
        assert report["counts"] == {
            "returned": 1,
            "total_matches": 1,
            "total_standing": "exact",
        }
        assert (
            report["timing"][
                "automatic_reconciliation_plus_search_count_elapsed_ms"
            ]
            >= 0
        )
        assert report["timing"]["operation_timing_standing"] == "inclusive"
        assert report["timing"]["provider_search_count_elapsed_ms"] is None
        assert (
            report["timing"]["provider_search_count_timing_standing"]
            == "unavailable_not_instrumented"
        )
        assert "search_with_count_elapsed_ms" not in serialized
        assert report["results"] == [
            {
                "episode_ref_digest": hashlib.sha256(episode_ref.encode()).hexdigest(),
                "corpus": report["enrollments"][0]["corpus"],
                "rank": 1,
            }
        ]
        assert report["opening"] == {
            "episode_ref_digest": hashlib.sha256(episode_ref.encode()).hexdigest(),
            "standing": "available",
            "content_digest": adapter.scan(
                enrollment, adapter.members(enrollment)[0]
            ).episodes[0].identity.body_digest,
        }
        assert report["reconciliation"]["bytes_read"] > 0
        assert report["reconciliation"]["elapsed_ms"] >= 0
        assert report["standing"][0]["sources"][0]["members"][0][
            "validation_age_seconds"
        ] >= 0
        assert report["index_growth"]["documents"] >= 1
        assert report["index_growth"]["serialized_bytes"] > 0
        assert report["purge_counts"]["episodes"] >= 1
        assert report["purge_counts"]["reconciliation"] >= 1
        assert report["purge_counts"]["supersessions"] == 0
        assert report["limitations"]
        assert not list(tmp_path.glob(".journey.json.*.tmp"))
    finally:
        _purge_prefix(db, prefix)


def test_purge_flag_rejects_non_test_corpus_before_reconciliation(tmp_path):
    db = get_database()
    ensure_contract_index(db)
    corpus_id = f"owner-corpus-{uuid4().hex}"
    source = tmp_path / "owner.jsonl"
    config = tmp_path / "sources.yaml"
    output = tmp_path / "journey.json"
    _write_fixture(source)
    _write_config(config, source, corpus_id, "owner-source")

    try:
        with pytest.raises(ValueError, match="restricted to corpus identifiers"):
            main(
                [
                    "--config",
                    str(config),
                    "--query",
                    "bounded evaluation",
                    "--limit",
                    "1",
                    "--output",
                    str(output),
                    "--purge-test-corpus",
                ]
            )

        assert _derived_count(db, corpus_id) == 0
        assert not output.exists()
    finally:
        _purge_prefix(db, corpus_id)
