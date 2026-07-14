from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llm_memory.contract import SearchRequest
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.opening import open_episode
from llm_memory.provider import EpisodicProvider, ProviderMeasurement, PurgeScope
from llm_memory.reconcile import ReconcileReport, WorkBudget
from llm_memory.sqlite_history import SQLITE_STRATEGY
from llm_memory.sqlite_provider import SQLITE_DESCRIPTOR, SQLiteProvider
from llm_memory.sqlite_store import SQLiteSchemaStanding


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _write_episode(path: Path, response: str) -> None:
    path.write_text(
        json.dumps(
            {
                "cycle": 1,
                "user_message": "local reason",
                "response_text": response,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _registry(path: Path, *, max_age: int = 3600) -> EnrollmentRegistry:
    return EnrollmentRegistry(
        (
            SourceEnrollment(
                corpus_id="local",
                source_id="source-a",
                adapter="taste_open_jsonl",
                boundary_version=1,
                canonicalization_version=1,
                locator=path,
                enabled=True,
                full_validation_max_age_seconds=max_age,
            ),
        )
    )


def test_sqlite_descriptor_declares_exact_retrieval_basis():
    assert SQLITE_DESCRIPTOR.as_dict() == {
        "provider": "sqlite",
        "implementation_version": "1",
        "strategies": ("lexical_bm25_fts5_porter_unicode61_v1",),
        "analyzer": "porter unicode61 remove_diacritics 2",
        "indexed_fields": ("user_message", "response", "state_text"),
        "match_semantics": "analyzed_any_segment_phrase",
        "score_ordering": "normalized_desc_episode_ref_asc",
        "raw_score_polarity": "lower_is_better",
    }


def test_sqlite_provider_satisfies_protocol_end_to_end(tmp_path):
    source_path = tmp_path / "source.jsonl"
    _write_episode(source_path, "the local reason")
    registry = _registry(source_path)
    provider: EpisodicProvider = SQLiteProvider(tmp_path / "episodes.sqlite3")

    assert provider.ensure() == {
        "provider": "sqlite",
        "schema_version": 1,
        "index_standing": "available",
    }
    response = provider.search(
        registry,
        SearchRequest.create("reason", ["local"], strategy=SQLITE_STRATEGY),
        WorkBudget(1_000_000, NOW),
    )

    assert response["strategy"] == SQLITE_STRATEGY
    assert provider.capabilities() == {
        "contract_versions": [1],
        "strategies": [SQLITE_STRATEGY],
        "supports_facets": False,
        "supports_continuation": False,
        "max_limit": 100,
        "retrieval_basis": SQLITE_DESCRIPTOR.as_dict(),
    }


def test_sqlite_provider_delegates_without_changing_arguments(tmp_path, monkeypatch):
    provider = SQLiteProvider(tmp_path / "episodes.sqlite3", busy_timeout_ms=17)
    registry = EnrollmentRegistry(())
    request = SearchRequest.create(
        "reason", ["local"], strategy=SQLITE_STRATEGY
    )
    budget = WorkBudget(100, NOW)
    enrollment = _registry(tmp_path / "unused.jsonl").sources[0]
    scope = PurgeScope(corpus_id="local", source_id="source-a")
    state_classes = frozenset({"episodes", "supersessions"})
    report = ReconcileReport((), 0, 0.0, False)
    measurement = ProviderMeasurement("sqlite", "available", {})
    calls = []

    monkeypatch.setattr(
        provider.store,
        "ensure",
        lambda: calls.append(("ensure",)) or SQLiteSchemaStanding(),
    )
    monkeypatch.setattr(
        provider.store,
        "resolve_supersession",
        lambda actual_enrollment, old_ref: calls.append(
            ("resolve", actual_enrollment, old_ref)
        )
        or "replacement-ref",
    )
    monkeypatch.setattr(
        "llm_memory.sqlite_provider.sqlite_reconcile",
        lambda store, actual_registry, actual_budget: calls.append(
            ("reconcile", store, actual_registry, actual_budget)
        )
        or report,
    )
    monkeypatch.setattr(
        "llm_memory.sqlite_provider.sqlite_search",
        lambda store, actual_registry, actual_request, actual_budget: calls.append(
            ("search", store, actual_registry, actual_request, actual_budget)
        )
        or {"results": []},
    )
    monkeypatch.setattr(
        "llm_memory.sqlite_provider.sqlite_purge",
        lambda store, actual_scope, actual_classes: calls.append(
            ("purge", store, actual_scope, actual_classes)
        )
        or {"episodes": 2, "supersessions": 1},
    )
    monkeypatch.setattr(
        "llm_memory.sqlite_provider.remove_provider_file",
        lambda store: calls.append(("remove", store)) or {"removed_paths": []},
    )
    monkeypatch.setattr(
        "llm_memory.sqlite_provider.sqlite_measure",
        lambda store, actual_scope: calls.append(("measure", store, actual_scope))
        or measurement,
    )

    assert provider.store.path == tmp_path / "episodes.sqlite3"
    assert provider.store.busy_timeout_ms == 17
    assert provider.ensure() == SQLiteSchemaStanding().as_dict()
    assert provider.reconcile(registry, budget) is report
    assert provider.search(registry, request, budget) == {"results": []}
    assert provider.resolve_supersession(enrollment, "old-ref") == "replacement-ref"
    assert provider.purge(scope, state_classes) == {
        "episodes": 2,
        "supersessions": 1,
    }
    assert provider.remove_all() == {"removed_paths": []}
    assert provider.measure(scope) is measurement
    assert calls == [
        ("ensure",),
        ("reconcile", provider.store, registry, budget),
        ("search", provider.store, registry, request, budget),
        ("resolve", enrollment, "old-ref"),
        ("purge", provider.store, scope, state_classes),
        ("remove", provider.store),
        ("measure", provider.store, scope),
    ]
    assert calls[2][3] is request


def test_sqlite_provider_opening_uses_its_supersession_only(tmp_path):
    source_path = tmp_path / "rewritten.jsonl"
    _write_episode(source_path, "old response")
    registry = _registry(source_path, max_age=1)
    provider = SQLiteProvider(tmp_path / "episodes.sqlite3")
    provider.ensure()
    provider.reconcile(registry, WorkBudget(1_000_000, NOW))
    old_ref = provider.store.active_episode_refs("local", "source-a")[0]

    _write_episode(source_path, "new response")
    provider.reconcile(
        registry, WorkBudget(1_000_000, NOW + timedelta(seconds=2))
    )
    provider.reconcile(
        registry, WorkBudget(1_000_000, NOW + timedelta(seconds=2))
    )

    response = open_episode(
        registry, old_ref, ["local"], provider.resolve_supersession
    )

    assert response["standing"] == "superseded"
    assert response["replacement_ref"] == provider.store.active_episode_refs(
        "local", "source-a"
    )[0]
