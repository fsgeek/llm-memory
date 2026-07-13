from __future__ import annotations

from typing import Any

from llm_memory.contract import (
    CONTRACT_VERSION,
    STRATEGY,
    ContractError,
    ProviderCapabilities,
    SearchRequest,
)
from llm_memory.contract_index import CONTRACT_VIEW, active_states
from llm_memory.enrollment import EnrollmentRegistry
from llm_memory.reconcile import WorkBudget, reconcile_registry
from llm_memory.search import _matched_field


_ANALYZER = "text_en"
_SNIPPET_LIMIT = 200

_SEARCH_AQL = """
LET matches = (
  FOR doc IN @@view
    SEARCH ANALYZER(
      doc.user_message IN TOKENS(@query, @analyzer) OR
      doc.response IN TOKENS(@query, @analyzer) OR
      doc.state_text IN TOKENS(@query, @analyzer),
      @analyzer
    )
    OPTIONS { waitForSync: true }
    FILTER doc.corpus_id IN @corpus_ids
    FILTER doc.generation_id IN @active_generations
    LET score = BM25(doc)
    SORT score DESC, doc.episode_ref ASC
    RETURN MERGE(doc, {score})
)
LET corpus_totals = (
  FOR doc IN matches
    COLLECT corpus_id = doc.corpus_id WITH COUNT INTO count
    SORT corpus_id
    RETURN {corpus_id, count}
)
RETURN {
  total_matches: LENGTH(matches),
  corpus_totals,
  results: SLICE(matches, 0, @limit)
}
"""


def provider_capabilities() -> dict[str, Any]:
    return ProviderCapabilities().as_dict()


def _validated_request(request: SearchRequest) -> SearchRequest:
    if not isinstance(request, SearchRequest):
        raise ContractError("request must be a SearchRequest")
    validated = SearchRequest.create(
        request.query,
        request.corpus_ids,
        limit=request.limit,
        strategy=request.strategy,
    )
    if validated.strategy != STRATEGY:
        raise ContractError(f"unsupported strategy: {validated.strategy!r}")
    return validated


def _scoped_registry(
    registry: EnrollmentRegistry, corpus_ids: tuple[str, ...]
) -> EnrollmentRegistry:
    unknown = [corpus_id for corpus_id in corpus_ids if corpus_id not in registry.known_corpora]
    if unknown:
        raise ContractError(f"unknown corpus: {unknown[0]!r}")

    enabled_corpora = {
        source.corpus_id for source in registry.sources if source.enabled
    }
    disabled = [corpus_id for corpus_id in corpus_ids if corpus_id not in enabled_corpora]
    if disabled:
        raise ContractError(f"disabled corpus: {disabled[0]!r}")

    requested = set(corpus_ids)
    return EnrollmentRegistry(
        tuple(
            source
            for source in registry.sources
            if source.enabled and source.corpus_id in requested
        )
    )


def _public_member(member: dict[str, Any]) -> dict[str, Any]:
    integrity = member.get("integrity") or {}
    return {
        "member_id": member["member_id"],
        "source_standing": member["source_standing"],
        "index_standing": member["index_standing"],
        "freshness": member["freshness"],
        "indexed_through": member["indexed_through"],
        "observed_source_end": member["observed_source_end"],
        "integrity": {
            "basis": integrity.get("basis"),
            "validated_at": integrity.get("validated_at"),
        },
    }


def _public_source(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": source["source_id"],
        "adapter": source["adapter"],
        "implementation_version": source["implementation_version"],
        "canonicalization_version": source["canonicalization_version"],
        "boundary_version": source["boundary_version"],
        "source_set_standing": source["source_set_standing"],
        "members": [_public_member(member) for member in source["members"]],
    }


def _public_result(document: dict[str, Any], query: str) -> dict[str, Any]:
    episode_ref = document["episode_ref"]
    _, qualified = episode_ref.split("://", 1)
    _, session_id, episode_id = qualified.split("/", 2)
    field = _matched_field(document, query)
    snippet = (document.get(field) or "")[:_SNIPPET_LIMIT] if field else ""
    return {
        "episode_ref": episode_ref,
        "corpus_id": document["corpus_id"],
        "session_id": session_id,
        "episode_id": episode_id,
        "timestamp": document.get("timestamp", ""),
        "score": document["score"],
        "match_attribution": {
            "field": field,
            "method": "provider_heuristic_v1",
            "standing": "heuristic",
        },
        "snippet": snippet,
    }


def search_history(
    db,
    registry: EnrollmentRegistry,
    request: SearchRequest,
    budget: WorkBudget,
) -> dict[str, Any]:
    validated = _validated_request(request)
    if not isinstance(registry, EnrollmentRegistry):
        raise ContractError("registry must be an EnrollmentRegistry")
    scoped_registry = _scoped_registry(registry, validated.corpus_ids)

    reconciliation = reconcile_registry(db, scoped_registry, budget)
    states = active_states(db, validated.corpus_ids)
    enabled_sources = {
        (source.corpus_id, source.source_id) for source in scoped_registry.sources
    }
    active_generations = sorted(
        {
            state["active_generation_id"]
            for state in states
            if (state["corpus_id"], state["source_id"]) in enabled_sources
        }
    )
    population = list(
        db.aql.execute(
            _SEARCH_AQL,
            bind_vars={
                "@view": CONTRACT_VIEW,
                "query": validated.query,
                "analyzer": _ANALYZER,
                "corpus_ids": list(validated.corpus_ids),
                "active_generations": active_generations,
                "limit": validated.limit,
            },
        )
    )[0]

    totals = {
        total["corpus_id"]: total["count"]
        for total in population["corpus_totals"]
    }
    reports = {
        report["corpus_id"]: report for report in reconciliation.corpus_standing
    }
    corpus_standing = []
    every_index_available = True
    for corpus_id in validated.corpus_ids:
        report = reports[corpus_id]
        sources = [_public_source(source) for source in report["sources"]]
        index_available = all(
            member["index_standing"] == "available"
            for source in sources
            for member in source["members"]
        )
        every_index_available = every_index_available and index_available
        corpus_standing.append(
            {
                "corpus_id": corpus_id,
                "indexed_matches": totals.get(corpus_id, 0),
                "match_standing": "exact" if index_available else "unknown",
                "sources": sources,
            }
        )

    bounded_documents = sorted(
        population["results"],
        key=lambda document: (-document["score"], document["episode_ref"]),
    )
    results = [
        _public_result(document, validated.query)
        for document in bounded_documents
    ]
    return {
        "contract_version": CONTRACT_VERSION,
        "query": validated.query,
        "strategy": validated.strategy,
        "match_semantics": "analyzed_any_token",
        "corpus_ids_considered": list(validated.corpus_ids),
        "corpus_standing": corpus_standing,
        "returned_count": len(results),
        "total_matches": population["total_matches"],
        "total_standing": "exact" if every_index_available else "unknown",
        "results": results,
    }
