import json
from uuid import uuid4

import pytest

from llm_memory import adapter_versions
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment

from provider_contract import SyntheticSourceFixture


@pytest.fixture
def enable_semantic_version(monkeypatch):
    def enable(adapter: str, *, boundary: int = 1, canonicalization: int = 1):
        supported = adapter_versions.SUPPORTED_SEMANTIC_VERSIONS[adapter]
        monkeypatch.setitem(
            adapter_versions.SUPPORTED_SEMANTIC_VERSIONS,
            adapter,
            supported | {(boundary, canonicalization)},
        )

    return enable


def _jsonl_bytes(records):
    return b"".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
        for record in records
    )


@pytest.fixture
def synthetic_source(tmp_path):
    prefix = f"portable-{uuid4().hex}"
    corpus_id = f"{prefix}-corpus"
    sentinel_corpus_id = f"{prefix}-sentinel"
    declarations = (
        (
            "primary",
            corpus_id,
            True,
            _jsonl_bytes(
                [
                    {
                        "cycle": cycle,
                        "timestamp": f"2026-07-14T12:00:0{cycle}Z",
                        "model": "synthetic-model",
                        "user_message": "portable decision primary authority",
                        "response_text": f"primary authority outcome {cycle}",
                        "state": {
                            "decision": {"status": "standing", "cycle": cycle},
                            "_activity_log": [{"operation": "synthetic-read"}],
                        },
                    }
                    for cycle in (1, 2, 3)
                ]
            ),
        ),
        (
            "secondary",
            corpus_id,
            True,
            _jsonl_bytes(
                [
                    {
                        "cycle": 1,
                        "user_message": "portable decision secondary authority",
                        "response_text": "secondary source result",
                    }
                ]
            ),
        ),
        ("empty", corpus_id, True, b""),
        (
            "disabled",
            corpus_id,
            False,
            _jsonl_bytes(
                [
                    {
                        "cycle": 1,
                        "user_message": "forbidden disabled marker",
                        "response_text": "must remain unauthorized",
                    }
                ]
            ),
        ),
        (
            "sentinel",
            sentinel_corpus_id,
            True,
            _jsonl_bytes(
                [
                    {
                        "cycle": 1,
                        "user_message": "sentinel isolation marker",
                        "response_text": "must survive scoped purge",
                    }
                ]
            ),
        ),
    )
    sources = []
    original_files = {}
    for source_id, source_corpus, enabled, source_bytes in declarations:
        path = tmp_path / f"{source_id}.jsonl"
        path.write_bytes(source_bytes)
        original_files[path] = source_bytes
        sources.append(
            SourceEnrollment(
                corpus_id=source_corpus,
                source_id=source_id,
                adapter="taste_open_jsonl",
                boundary_version=1,
                canonicalization_version=1,
                locator=path,
                enabled=enabled,
                full_validation_max_age_seconds=1,
            )
        )

    yield SyntheticSourceFixture(
        registry=EnrollmentRegistry(tuple(sources)),
        path=sources[0].locator,
        original_bytes=original_files[sources[0].locator],
        original_files=original_files,
        corpus_id=corpus_id,
        sentinel_corpus_id=sentinel_corpus_id,
    )
