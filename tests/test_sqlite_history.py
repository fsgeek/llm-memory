from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llm_memory.contract import ContractError, SearchRequest
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.reconcile import WorkBudget
from llm_memory.sqlite_history import (
    SQLITE_STRATEGY,
    encode_fts5_query,
    search_history,
)
from llm_memory.sqlite_reconcile import reconcile_registry
from llm_memory.sqlite_store import SQLiteStore


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def sqlite_store(tmp_path):
    store = SQLiteStore(tmp_path / "episodes.sqlite3", busy_timeout_ms=50)
    store.ensure()
    return store


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_bytes(
        b"".join(
            json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
            for record in records
        )
    )


def _taste(
    cycle: int | str,
    response: str,
    *,
    question: str = "synthetic history question",
    state: dict | None = None,
) -> dict:
    return {
        "cycle": cycle,
        "timestamp": "2026-07-14T11:59:00Z",
        "user_message": question,
        "response_text": response,
        "state": state or {},
    }


def _enrollment(corpus_id: str, source_id: str, locator: Path) -> SourceEnrollment:
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


def _request(query: str, *corpus_ids: str, limit: int = 10) -> SearchRequest:
    return SearchRequest.create(
        query,
        corpus_ids,
        limit=limit,
        strategy=SQLITE_STRATEGY,
    )


def _search(
    store: SQLiteStore,
    registry: EnrollmentRegistry,
    query: str,
    *corpus_ids: str,
    limit: int = 10,
) -> dict:
    return search_history(
        store,
        registry,
        _request(query, *corpus_ids, limit=limit),
        WorkBudget(1_000_000, NOW),
    )


@pytest.mark.parametrize(
    ("query", "encoded"),
    [
        ("why cache", '"why" OR "cache"'),
        ('why OR "drop"', '"why" OR "OR" OR """drop"""'),
        ("caf\u00e9\tdecision", '"caf\u00e9" OR "decision"'),
    ],
)
def test_encode_fts5_query_treats_input_as_text(query, encoded):
    assert encode_fts5_query(query) == encoded


def test_unknown_strategy_fails_before_reconciliation(sqlite_store, tmp_path, monkeypatch):
    path = tmp_path / "unknown-strategy.jsonl"
    _write_jsonl(path, [_taste(1, "reason")])
    registry = EnrollmentRegistry((_enrollment("local", "taste", path),))
    monkeypatch.setattr(
        "llm_memory.sqlite_history.reconcile_registry",
        lambda *args: pytest.fail("unsupported strategy reached reconciliation"),
    )

    with pytest.raises(ContractError, match="unsupported strategy"):
        search_history(
            sqlite_store,
            registry,
            SearchRequest.create("reason", ["local"]),
            WorkBudget(1_000_000, NOW),
        )


def test_search_reconciles_requested_scope_before_snapshot_with_passed_budget(
    sqlite_store, tmp_path, monkeypatch
):
    path = tmp_path / "ordering.jsonl"
    _write_jsonl(path, [_taste(1, "reason after reconciliation")])
    source = _enrollment("local", "taste", path)
    ignored_path = tmp_path / "ignored.jsonl"
    _write_jsonl(ignored_path, [_taste(1, "reason outside scope")])
    ignored = _enrollment("ignored", "taste", ignored_path)
    registry = EnrollmentRegistry((ignored, source))
    budget = WorkBudget(1_000_000, NOW)
    calls = []
    real_reconcile = reconcile_registry

    def observed_reconcile(store, scoped_registry, actual_budget):
        calls.append((tuple(item.corpus_id for item in scoped_registry.sources), actual_budget))
        return real_reconcile(store, scoped_registry, actual_budget)

    monkeypatch.setattr(
        "llm_memory.sqlite_history.reconcile_registry", observed_reconcile
    )

    response = search_history(
        sqlite_store, registry, _request("reason", "local"), budget
    )

    assert calls == [(("local",), budget)]
    assert response["total_matches"] == 1
    assert response["results"][0]["corpus_id"] == "local"


def test_search_preserves_public_shape_and_normalizes_bm25_order(
    sqlite_store, tmp_path
):
    path = tmp_path / "results.jsonl"
    long_response = "reason " + "tail " * 80
    _write_jsonl(path, [_taste(2, long_response), _taste(1, long_response)])
    registry = EnrollmentRegistry((_enrollment("local", "taste", path),))

    response = _search(sqlite_store, registry, "  reason  ", "local")

    assert response == response | {
        "contract_version": 1,
        "query": "reason",
        "strategy": SQLITE_STRATEGY,
        "match_semantics": "analyzed_any_segment_phrase",
        "corpus_ids_considered": ["local"],
        "returned_count": 2,
        "total_matches": 2,
        "total_standing": "exact",
    }
    assert response["results"] == sorted(
        response["results"],
        key=lambda hit: (-hit["score"], hit["episode_ref"]),
    )
    assert all(hit["score"] > 0 for hit in response["results"])
    assert all(
        set(hit)
        == {
            "episode_ref",
            "corpus_id",
            "session_id",
            "episode_id",
            "timestamp",
            "score",
            "match_attribution",
            "snippet",
        }
        for hit in response["results"]
    )
    assert all(
        hit["match_attribution"]
        == {
            "field": "response",
            "method": "provider_heuristic_v1",
            "standing": "heuristic",
        }
        for hit in response["results"]
    )
    assert all(len(hit["snippet"]) == 200 for hit in response["results"])
    corpus = response["corpus_standing"][0]
    assert corpus["indexed_matches"] == 2
    assert corpus["match_standing"] == "exact"
    assert set(corpus) == {
        "corpus_id",
        "indexed_matches",
        "match_standing",
        "sources",
    }


def test_query_segments_are_or_phrases_and_user_syntax_is_literal(
    sqlite_store, tmp_path
):
    path = tmp_path / "literal-query.jsonl"
    _write_jsonl(
        path,
        [
            _taste(1, "why this happened"),
            _taste(2, "drop is ordinary prose"),
            _taste(3, "unrelated answer"),
        ],
    )
    registry = EnrollmentRegistry((_enrollment("local", "taste", path),))

    response = _search(sqlite_store, registry, 'why OR "drop"', "local")

    assert response["total_matches"] == 2
    assert {hit["snippet"] for hit in response["results"]} == {
        "why this happened",
        "drop is ordinary prose",
    }


def test_fts_indexes_only_the_three_declared_prose_fields(sqlite_store, tmp_path):
    path = tmp_path / "indexed-fields.jsonl"
    _write_jsonl(
        path,
        [
            _taste("metadataonly", "plain response", question="plain question"),
            _taste(2, "responseonly marker", question="plain question"),
            _taste(3, "plain response", question="useronly marker"),
            _taste(4, "plain response", state={"decision": "stateonly marker"}),
        ],
    )
    registry = EnrollmentRegistry((_enrollment("local", "taste", path),))

    metadata = _search(sqlite_store, registry, "metadataonly", "local")
    response = _search(sqlite_store, registry, "responseonly", "local")
    user = _search(sqlite_store, registry, "useronly", "local")
    state = _search(sqlite_store, registry, "stateonly", "local")

    assert metadata["total_matches"] == 0
    assert [item["total_matches"] for item in (response, user, state)] == [1, 1, 1]


def test_inactive_generation_is_neither_searched_nor_counted(sqlite_store, tmp_path):
    path = tmp_path / "inactive.jsonl"
    _write_jsonl(path, [_taste(1, "active prose")])
    source = _enrollment("local", "taste", path)
    registry = EnrollmentRegistry((source,))
    report = reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))
    with sqlite_store.write_transaction() as connection:
        connection.execute(
            """
            INSERT INTO episode_documents(
              storage_key, corpus_id, source_id, member_id, generation_id,
              episode_ref, reference_key, timestamp, user_message, response,
              state_text, document_json
            )
            SELECT
              'inactive-storage', corpus_id, source_id, member_id, 'inactive-generation',
              'episode://local/inactive/episode', 'inactive-reference', timestamp,
              user_message, 'inactiveonly prose', state_text, document_json
            FROM episode_documents
            LIMIT 1
            """
        )

    response = _search(sqlite_store, registry, "inactiveonly", "local")

    assert report.corpus_standing[0]["sources"][0]["members"][0][
        "index_standing"
    ] == "available"
    assert response["returned_count"] == 0
    assert response["total_matches"] == 0
    assert response["total_standing"] == "exact"


@pytest.mark.parametrize("corruption", ["document", "fts", "state"])
def test_unbacked_active_generation_has_unknown_exact_population(
    sqlite_store, tmp_path, monkeypatch, corruption
):
    path = tmp_path / f"corrupt-{corruption}.jsonl"
    _write_jsonl(path, [_taste(1, "reason survives only when backed")])
    source = _enrollment("local", "taste", path)
    registry = EnrollmentRegistry((source,))
    report = reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))
    with sqlite_store.write_transaction() as connection:
        if corruption == "document":
            connection.execute("DELETE FROM episode_documents")
        elif corruption == "fts":
            connection.execute("DELETE FROM episode_fts")
        else:
            connection.execute(
                "UPDATE source_states SET state_json = "
                "json_set(state_json, '$.active_generation_integrity', 'invalid')"
            )
    monkeypatch.setattr(
        "llm_memory.sqlite_history.reconcile_registry", lambda *args: report
    )

    response = _search(sqlite_store, registry, "reason", "local")

    assert response["returned_count"] == 0
    assert response["total_matches"] is None
    assert response["total_standing"] == "unknown"
    assert response["corpus_standing"][0]["indexed_matches"] is None
    assert response["corpus_standing"][0]["match_standing"] == "unknown"


def test_one_unbacked_corpus_does_not_make_partial_aggregate_exact(
    sqlite_store, tmp_path, monkeypatch
):
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    _write_jsonl(first_path, [_taste(1, "reason first")])
    _write_jsonl(second_path, [_taste(1, "reason second")])
    first = _enrollment("first", "taste", first_path)
    second = _enrollment("second", "taste", second_path)
    registry = EnrollmentRegistry((first, second))
    report = reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))
    state = sqlite_store.member_state(second, second.source_id)
    with sqlite_store.write_transaction() as connection:
        connection.execute(
            "DELETE FROM episode_fts WHERE generation_id = ?",
            (state["active_generation_id"],),
        )
    monkeypatch.setattr(
        "llm_memory.sqlite_history.reconcile_registry", lambda *args: report
    )

    response = _search(sqlite_store, registry, "reason", "second", "first")

    assert [item["corpus_id"] for item in response["corpus_standing"]] == [
        "second",
        "first",
    ]
    assert [item["indexed_matches"] for item in response["corpus_standing"]] == [
        None,
        1,
    ]
    assert [item["match_standing"] for item in response["corpus_standing"]] == [
        "unknown",
        "exact",
    ]
    assert response["total_matches"] is None
    assert response["total_standing"] == "unknown"
    assert [item["corpus_id"] for item in response["results"]] == ["first"]


def test_count_and_results_share_one_read_snapshot(sqlite_store, tmp_path, monkeypatch):
    path = tmp_path / "snapshot.jsonl"
    _write_jsonl(path, [_taste(1, "snapshotreason original")])
    source = _enrollment("local", "taste", path)
    registry = EnrollmentRegistry((source,))
    report = reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))
    state = sqlite_store.member_state(source, source.source_id)
    original_read_transaction = sqlite_store.read_transaction
    writer_committed = False

    @contextmanager
    def observed_read_transaction():
        nonlocal writer_committed
        with original_read_transaction() as reader:
            def commit_writer(statement):
                nonlocal writer_committed
                if "sqlite_history_results" not in statement or writer_committed:
                    return
                reader.set_trace_callback(None)
                with sqlite_store.write_transaction() as writer:
                    writer.execute(
                        """
                        INSERT INTO episode_documents(
                          storage_key, corpus_id, source_id, member_id, generation_id,
                          episode_ref, reference_key, timestamp, user_message, response,
                          state_text, document_json
                        )
                        SELECT
                          'writer-storage', corpus_id, source_id, member_id, generation_id,
                          'episode://local/writer/episode', 'writer-reference', timestamp,
                          user_message, 'snapshotreason writer', state_text, document_json
                        FROM episode_documents
                        WHERE generation_id = ?
                        LIMIT 1
                        """,
                        (state["active_generation_id"],),
                    )
                writer_committed = True

            reader.set_trace_callback(commit_writer)
            yield reader

    monkeypatch.setattr(sqlite_store, "read_transaction", observed_read_transaction)
    monkeypatch.setattr(
        "llm_memory.sqlite_history.reconcile_registry", lambda *args: report
    )

    response = _search(sqlite_store, registry, "snapshotreason", "local")

    assert writer_committed is True
    assert response["total_matches"] == 1
    assert response["returned_count"] == 1
    assert response["results"][0]["snippet"] == "snapshotreason original"
    with original_read_transaction() as connection:
        assert connection.execute(
            "SELECT count(*) FROM episode_documents WHERE generation_id = ?",
            (state["active_generation_id"],),
        ).fetchone()[0] == 2
