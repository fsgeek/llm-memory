from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from llm_memory.contract import (
    STRATEGY,
    ContractError,
    SearchRequest,
)
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    SOURCE_STATES,
    SUPERSESSIONS,
    active_states,
    ensure_contract_index,
)
from llm_memory.db import get_database
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.history import provider_capabilities, search_history
from llm_memory.lifecycle import purge_derived
from llm_memory.reconcile import ReconcileReport, WorkBudget


NOW = datetime(2026, 7, 12, 18, 30, tzinfo=UTC)


class FailBeforeAQL:
    @property
    def aql(self):
        raise AssertionError("invalid scope reached AQL")


@pytest.fixture
def history_storage():
    db = get_database()
    ensure_contract_index(db)
    prefix = f"history-test-{uuid4().hex}"
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


def enrollment(
    corpus_id: str,
    source_id: str,
    adapter: str,
    locator: Path,
    *,
    enabled: bool = True,
) -> SourceEnrollment:
    return SourceEnrollment(
        corpus_id=corpus_id,
        source_id=source_id,
        adapter=adapter,
        boundary_version=1,
        canonicalization_version=1,
        locator=locator,
        enabled=enabled,
        full_validation_max_age_seconds=3600,
    )


def write_jsonl(path: Path, records: list[dict], *, final_newline: bool = True) -> None:
    data = b"\n".join(json.dumps(record).encode() for record in records)
    path.write_bytes(data + (b"\n" if final_newline else b""))


def taste(cycle: int, response: str, *, question: str = "history question") -> dict:
    return {
        "cycle": cycle,
        "timestamp": "2026-07-12T18:29:10Z",
        "user_message": question,
        "response_text": response,
    }


def gateway(session: str, response: str) -> dict:
    return {
        "type": "request_metrics",
        "session_id": session,
        "timestamp": "2026-07-12T18:29:10Z",
        "messages_full": [{"role": "user", "content": "history question"}],
        "response_text": response,
    }


def request(*corpus_ids: str, limit: int = 10) -> SearchRequest:
    return SearchRequest.create("heliotrope marker", corpus_ids, limit=limit)


def run_search(db, registry: EnrollmentRegistry, search_request: SearchRequest) -> dict:
    return search_history(db, registry, search_request, WorkBudget(1_000_000, NOW))


@pytest.mark.parametrize(
    "search_request",
    [
        SearchRequest("query", (), 10, STRATEGY),
        SearchRequest("query", ("corpus-a", "corpus-a"), 10, STRATEGY),
        SearchRequest("query", ("*",), 10, STRATEGY),
    ],
)
def test_nonconcrete_scope_fails_before_aql(search_request):
    registry = EnrollmentRegistry(
        (
            enrollment(
                "corpus-a", "source-a", "taste_open_jsonl", Path("/unused")
            ),
        )
    )

    with pytest.raises(ContractError):
        search_history(FailBeforeAQL(), registry, search_request, WorkBudget(1, NOW))


def test_unknown_and_disabled_corpora_fail_before_aql():
    registry = EnrollmentRegistry(
        (
            enrollment(
                "disabled", "source-a", "taste_open_jsonl", Path("/unused"), enabled=False
            ),
        )
    )

    with pytest.raises(ContractError, match="unknown corpus"):
        search_history(
            FailBeforeAQL(), registry, SearchRequest.create("query", ["unknown"]), WorkBudget(1, NOW)
        )
    with pytest.raises(ContractError, match="disabled corpus"):
        search_history(
            FailBeforeAQL(), registry, SearchRequest.create("query", ["disabled"]), WorkBudget(1, NOW)
        )


@pytest.mark.parametrize("contract_version", [True, 0, 2])
def test_invalid_direct_request_version_fails_before_aql(contract_version):
    registry = EnrollmentRegistry(
        (
            enrollment(
                "corpus-a", "source-a", "taste_open_jsonl", Path("/unused")
            ),
        )
    )
    search_request = SearchRequest(
        "query", ("corpus-a",), 10, STRATEGY, contract_version
    )

    with pytest.raises(ContractError, match="contract_version must be 1"):
        search_history(FailBeforeAQL(), registry, search_request, WorkBudget(1, NOW))


def test_capabilities_and_search_response_are_contract_version_one(
    history_storage, tmp_path
):
    db, corpus_id = history_storage
    path = tmp_path / "version.jsonl"
    write_jsonl(path, [taste(1, "heliotrope marker")])
    registry = EnrollmentRegistry(
        (enrollment(corpus_id, "taste", "taste_open_jsonl", path),)
    )
    search_request = request(corpus_id)

    response = run_search(db, registry, search_request)

    assert search_request.contract_version == 1
    assert response["contract_version"] == 1
    assert provider_capabilities() == {
        "contract_versions": [1],
        "strategies": ["lexical_bm25_text_en_v1"],
        "supports_facets": False,
        "supports_continuation": False,
        "max_limit": 100,
    }


def test_limit_slices_results_but_counts_the_exact_indexed_population(
    history_storage, tmp_path
):
    db, corpus_id = history_storage
    path = tmp_path / "population.jsonl"
    write_jsonl(
        path,
        [
            taste(1, "heliotrope marker same words"),
            taste(2, "heliotrope marker same words"),
            taste(3, "heliotrope marker same words"),
        ],
    )
    registry = EnrollmentRegistry(
        (enrollment(corpus_id, "taste", "taste_open_jsonl", path),)
    )

    response = run_search(db, registry, request(corpus_id, limit=1))

    assert response["returned_count"] == 1
    assert response["total_matches"] == 3
    assert response["total_standing"] == "exact"
    assert response["corpus_standing"][0]["indexed_matches"] == 3
    assert response["corpus_standing"][0]["match_standing"] == "exact"
    assert len(response["results"]) == 1


def test_results_use_qualified_refs_stable_tie_break_and_bounded_heuristic_snippets(
    history_storage, tmp_path
):
    db, corpus_id = history_storage
    path = tmp_path / "results.jsonl"
    response_text = "heliotrope marker " + "tail " * 80
    write_jsonl(path, [taste(2, response_text), taste(1, response_text)])
    registry = EnrollmentRegistry(
        (enrollment(corpus_id, "taste", "taste_open_jsonl", path),)
    )

    response = run_search(db, registry, request(corpus_id))
    results = response["results"]

    assert [(result["score"], result["episode_ref"]) for result in results] == sorted(
        ((result["score"], result["episode_ref"]) for result in results),
        key=lambda item: (-item[0], item[1]),
    )
    assert all(result["episode_ref"].startswith(f"episode://{corpus_id}/") for result in results)
    assert all(result["session_id"] and result["episode_id"] for result in results)
    assert all(result["match_attribution"] == {
        "field": "response",
        "method": "provider_heuristic_v1",
        "standing": "heuristic",
    } for result in results)
    assert all(len(result["snippet"]) <= 200 for result in results)
    assert all("_key" not in result and "key" not in result for result in results)


def test_different_adapters_share_one_corpus_without_collapsing_member_standing(
    history_storage, tmp_path
):
    db, corpus_id = history_storage
    taste_path = tmp_path / "taste.jsonl"
    gateway_path = tmp_path / "gateway.jsonl"
    write_jsonl(taste_path, [taste(1, "heliotrope marker taste")])
    write_jsonl(gateway_path, [gateway("gateway-session", "heliotrope marker gateway")])
    registry = EnrollmentRegistry(
        (
            enrollment(corpus_id, "taste", "taste_open_jsonl", taste_path),
            enrollment(corpus_id, "gateway", "gateway_jsonl", gateway_path),
        )
    )

    response = run_search(db, registry, request(corpus_id))
    sources = response["corpus_standing"][0]["sources"]

    assert response["total_matches"] == 2
    assert [(source["source_id"], source["adapter"]) for source in sources] == [
        ("gateway", "gateway_jsonl"),
        ("taste", "taste_open_jsonl"),
    ]
    assert all(len(source["members"]) == 1 for source in sources)
    assert all(source["members"][0]["index_standing"] == "available" for source in sources)
    assert all("freshness" not in source for source in sources)


def test_requested_corpora_keep_independent_counts_and_standing(history_storage, tmp_path):
    db, prefix = history_storage
    first_corpus = prefix + "-first"
    second_corpus = prefix + "-second"
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    write_jsonl(first_path, [taste(1, "heliotrope marker first")])
    write_jsonl(
        second_path,
        [taste(1, "heliotrope marker second"), taste(2, "heliotrope marker second")],
    )
    registry = EnrollmentRegistry(
        (
            enrollment(first_corpus, "first", "taste_open_jsonl", first_path),
            enrollment(second_corpus, "second", "taste_open_jsonl", second_path),
        )
    )

    response = run_search(db, registry, request(second_corpus, first_corpus))

    assert response["corpus_ids_considered"] == [second_corpus, first_corpus]
    assert [standing["corpus_id"] for standing in response["corpus_standing"]] == [
        second_corpus,
        first_corpus,
    ]
    assert [standing["indexed_matches"] for standing in response["corpus_standing"]] == [2, 1]
    assert {result["corpus_id"] for result in response["results"]} == {
        first_corpus,
        second_corpus,
    }


def test_unavailable_member_index_keeps_hits_but_makes_population_unknown(monkeypatch):
    corpus_id = "corpus-a"
    registry = EnrollmentRegistry(
        (
            enrollment(corpus_id, "available", "taste_open_jsonl", Path("/available")),
            enrollment(corpus_id, "missing", "taste_open_jsonl", Path("/missing")),
        )
    )
    available_member = {
        "member_id": "available",
        "source_standing": "available",
        "index_standing": "available",
        "freshness": "current",
        "indexed_through": {"kind": "byte_offset", "value": 12},
        "observed_source_end": {"kind": "byte_offset", "value": 12},
        "integrity": {"basis": "full_digest", "validated_at": "2026-07-12T18:30:00Z"},
    }
    unavailable_member = available_member | {
        "member_id": "missing",
        "source_standing": "missing",
        "index_standing": "unavailable",
        "freshness": "unavailable",
        "indexed_through": {"kind": "byte_offset", "value": 0},
        "observed_source_end": {"kind": "byte_offset", "value": 0},
    }
    source_shape = {
        "adapter": "taste_open_jsonl",
        "implementation_version": "1",
        "canonicalization_version": 1,
        "boundary_version": 1,
        "source_set_standing": "available",
    }
    report = ReconcileReport(
        corpus_standing=(
            {
                "corpus_id": corpus_id,
                "sources": (
                    source_shape | {"source_id": "available", "members": (available_member,)},
                    source_shape | {"source_id": "missing", "members": (unavailable_member,)},
                ),
            },
        ),
        bytes_read=0,
        elapsed_ms=0.0,
        work_exhausted=False,
    )

    class AQL:
        def execute(self, query, *, bind_vars):
            assert "SORT score DESC, doc.episode_ref ASC" in query
            return iter(
                [
                    {
                        "total_matches": 1,
                        "corpus_totals": [{"corpus_id": corpus_id, "count": 1}],
                        "results": [
                            {
                                "episode_ref": "episode://corpus-a/c2Vzc2lvbg/ZXBpc29kZQ",
                                "corpus_id": corpus_id,
                                "timestamp": "",
                                "score": 1.0,
                                "response": "heliotrope marker",
                                "user_message": "",
                                "state_text": "",
                            }
                        ],
                    }
                ]
            )

    class DB:
        aql = AQL()

    monkeypatch.setattr("llm_memory.history.reconcile_registry", lambda *args: report)

    response = search_history(DB(), registry, request(corpus_id), WorkBudget(1, NOW))
    corpus = response["corpus_standing"][0]
    missing = next(source for source in corpus["sources"] if source["source_id"] == "missing")

    assert response["returned_count"] == 1
    assert response["results"][0]["corpus_id"] == corpus_id
    assert response["total_standing"] == "unknown"
    assert corpus["match_standing"] == "unknown"
    assert missing["members"][0]["index_standing"] == "unavailable"


def test_active_generations_are_limited_to_enabled_sources(monkeypatch):
    corpus_id = "corpus-a"
    enabled = enrollment(corpus_id, "enabled", "taste_open_jsonl", Path("/enabled"))
    disabled = enrollment(
        corpus_id, "disabled", "taste_open_jsonl", Path("/disabled"), enabled=False
    )
    registry = EnrollmentRegistry((enabled, disabled))
    report = ReconcileReport(
        corpus_standing=(
            {
                "corpus_id": corpus_id,
                "sources": (
                    {
                        "source_id": "enabled",
                        "adapter": "taste_open_jsonl",
                        "implementation_version": "1",
                        "canonicalization_version": 1,
                        "boundary_version": 1,
                        "source_set_standing": "available",
                        "members": (
                            {
                                "member_id": "enabled",
                                "source_standing": "available",
                                "index_standing": "available",
                                "freshness": "current",
                                "indexed_through": {"kind": "byte_offset", "value": 1},
                                "observed_source_end": {"kind": "byte_offset", "value": 1},
                                "integrity": {"basis": "full_digest", "validated_at": None},
                            },
                        ),
                    },
                ),
            },
        ),
        bytes_read=0,
        elapsed_ms=0.0,
        work_exhausted=False,
    )

    class AQL:
        def execute(self, query, *, bind_vars):
            assert "FOR state IN @@states" in query
            assert bind_vars["enabled_source_keys"] == [
                f"{corpus_id}\0enabled"
            ]
            return iter([{"total_matches": 0, "corpus_totals": [], "results": []}])

    class DB:
        aql = AQL()

    monkeypatch.setattr("llm_memory.history.reconcile_registry", lambda *args: report)

    response = search_history(DB(), registry, request(corpus_id), WorkBudget(1, NOW))

    assert response["returned_count"] == 0


@pytest.mark.parametrize("freshness", ["stale", "tail_validated", "incomplete"])
def test_noncurrent_available_indexes_remain_searchable_with_member_standing(
    monkeypatch, freshness
):
    corpus_id = "corpus-a"
    source = enrollment(corpus_id, "source-a", "taste_open_jsonl", Path("/unused"))
    registry = EnrollmentRegistry((source,))
    member = {
        "member_id": "member-a",
        "source_standing": "available",
        "index_standing": "available",
        "freshness": freshness,
        "indexed_through": {"kind": "byte_offset", "value": 12},
        "observed_source_end": {"kind": "byte_offset", "value": 20},
        "integrity": {"basis": "full_digest", "validated_at": None},
    }
    report = ReconcileReport(
        corpus_standing=(
            {
                "corpus_id": corpus_id,
                "sources": (
                    {
                        "source_id": "source-a",
                        "adapter": "taste_open_jsonl",
                        "implementation_version": "1",
                        "canonicalization_version": 1,
                        "boundary_version": 1,
                        "source_set_standing": "available",
                        "members": (member,),
                    },
                ),
            },
        ),
        bytes_read=0,
        elapsed_ms=0.0,
        work_exhausted=False,
    )

    class AQL:
        def execute(self, query, *, bind_vars):
            assert bind_vars["enabled_source_keys"] == [
                f"{corpus_id}\0source-a"
            ]
            return iter(
                [
                    {
                        "total_matches": 1,
                        "corpus_totals": [{"corpus_id": corpus_id, "count": 1}],
                        "results": [
                            {
                                "episode_ref": "episode://corpus-a/c2Vzc2lvbg/ZXBpc29kZQ",
                                "corpus_id": corpus_id,
                                "timestamp": "",
                                "score": 1.0,
                                "response": "heliotrope marker",
                                "user_message": "",
                                "state_text": "",
                            }
                        ],
                    }
                ]
            )

    class DB:
        aql = AQL()

    monkeypatch.setattr("llm_memory.history.reconcile_registry", lambda *args: report)

    response = search_history(DB(), registry, request(corpus_id), WorkBudget(1, NOW))

    assert response["returned_count"] == 1
    assert response["corpus_standing"][0]["sources"][0]["members"][0]["freshness"] == freshness
    assert response["corpus_standing"][0]["match_standing"] == "exact"


def test_search_reconciles_before_its_population_query(history_storage, tmp_path):
    db, corpus_id = history_storage
    path = tmp_path / "automatic.jsonl"
    write_jsonl(path, [taste(1, "heliotrope marker created before search")])
    registry = EnrollmentRegistry(
        (enrollment(corpus_id, "taste", "taste_open_jsonl", path),)
    )

    assert active_states(db, (corpus_id,)) == ()
    response = run_search(db, registry, request(corpus_id))

    assert response["returned_count"] == 1
    assert len(active_states(db, (corpus_id,))) == 1


def test_episode_only_purge_rebuilds_before_reporting_exact_population(
    history_storage, tmp_path
):
    db, corpus_id = history_storage
    path = tmp_path / "purged-episodes.jsonl"
    write_jsonl(path, [taste(1, "heliotrope marker survives derived purge")])
    registry = EnrollmentRegistry(
        (enrollment(corpus_id, "taste", "taste_open_jsonl", path),)
    )
    assert run_search(db, registry, request(corpus_id))["total_matches"] == 1
    assert purge_derived(
        db, corpus_id, classes=frozenset({"episodes"})
    ) == {"episodes": 1}

    rebuilt = run_search(db, registry, request(corpus_id))

    assert rebuilt["total_matches"] == 1
    assert rebuilt["total_standing"] == "exact"
    assert rebuilt["corpus_standing"][0]["match_standing"] == "exact"
