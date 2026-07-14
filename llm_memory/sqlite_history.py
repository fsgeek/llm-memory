from __future__ import annotations

from typing import Any

from llm_memory.contract import CONTRACT_VERSION, ContractError, SearchRequest
from llm_memory.enrollment import EnrollmentRegistry
from llm_memory.history import _public_result, _public_source, _scoped_registry
from llm_memory.reconcile import WorkBudget
from llm_memory.sqlite_reconcile import reconcile_registry
from llm_memory.sqlite_store import SQLiteStore


SQLITE_STRATEGY = "lexical_bm25_fts5_porter_unicode61_v1"

_MATCH_SEMANTICS = "analyzed_any_segment_phrase"

_BACKING_SQL = """
WITH expected(corpus_id, source_id, member_id) AS (
  VALUES {expected_members}
)
SELECT
  expected.corpus_id,
  expected.source_id,
  expected.member_id,
  json_extract(state.state_json, '$.active_generation_id') AS generation_id,
  json_extract(state.state_json, '$.active_generation_integrity') AS integrity,
  json_extract(state.state_json, '$.episode_count') AS episode_count,
  (
    SELECT count(*)
    FROM episode_documents AS document
    WHERE document.corpus_id = expected.corpus_id
      AND document.source_id = expected.source_id
      AND document.member_id = expected.member_id
      AND document.generation_id =
        json_extract(state.state_json, '$.active_generation_id')
  ) AS document_count,
  (
    SELECT count(*)
    FROM episode_fts
    WHERE episode_fts.corpus_id = expected.corpus_id
      AND episode_fts.generation_id =
        json_extract(state.state_json, '$.active_generation_id')
  ) AS fts_count
FROM expected
LEFT JOIN source_states AS state
  ON state.corpus_id = expected.corpus_id
 AND state.source_id = expected.source_id
 AND state.member_id = expected.member_id
ORDER BY expected.corpus_id, expected.source_id, expected.member_id
"""

_POPULATION_SQL = """
WITH backed(corpus_id, source_id, member_id, generation_id) AS (
  VALUES {backed_generations}
)
SELECT document.corpus_id, count(*) AS match_count
FROM episode_fts
JOIN episode_documents AS document ON document.rowid = episode_fts.rowid
JOIN backed
  ON backed.corpus_id = document.corpus_id
 AND backed.source_id = document.source_id
 AND backed.member_id = document.member_id
 AND backed.generation_id = document.generation_id
WHERE episode_fts MATCH ?
GROUP BY document.corpus_id
ORDER BY document.corpus_id
"""

_RESULTS_SQL = """
/* sqlite_history_results */
WITH backed(corpus_id, source_id, member_id, generation_id) AS (
  VALUES {backed_generations}
)
SELECT document.*, -bm25(episode_fts) AS score
FROM episode_fts
JOIN episode_documents AS document ON document.rowid = episode_fts.rowid
JOIN backed
  ON backed.corpus_id = document.corpus_id
 AND backed.source_id = document.source_id
 AND backed.member_id = document.member_id
 AND backed.generation_id = document.generation_id
WHERE episode_fts MATCH ?
ORDER BY score DESC, document.episode_ref ASC
LIMIT ?
"""


def encode_fts5_query(query: str) -> str:
    segments = query.strip().split()
    return " OR ".join(
        f'"{segment.replace(chr(34), chr(34) * 2)}"' for segment in segments
    )


def _validated_request(request: SearchRequest) -> SearchRequest:
    if not isinstance(request, SearchRequest):
        raise ContractError("request must be a SearchRequest")
    validated = SearchRequest.create(
        request.query,
        request.corpus_ids,
        limit=request.limit,
        strategy=request.strategy,
        contract_version=request.contract_version,
    )
    if validated.strategy != SQLITE_STRATEGY:
        raise ContractError(f"unsupported strategy: {validated.strategy!r}")
    return validated


def _expected_members(reconciliation) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (corpus["corpus_id"], source["source_id"], member["member_id"])
        for corpus in reconciliation.corpus_standing
        for source in corpus["sources"]
        for member in source["members"]
    )


def _values_sql(width: int, count: int) -> str:
    row = f"({', '.join('?' for _ in range(width))})"
    return ", ".join(row for _ in range(count))


def _backed_generations(
    connection, expected_members: tuple[tuple[str, str, str], ...]
) -> tuple[tuple[str, str, str, str], ...]:
    if not expected_members:
        return ()
    rows = connection.execute(
        _BACKING_SQL.format(
            expected_members=_values_sql(3, len(expected_members))
        ),
        tuple(value for member in expected_members for value in member),
    ).fetchall()
    backed = []
    for row in rows:
        generation_id = row["generation_id"]
        episode_count = row["episode_count"]
        if (
            isinstance(generation_id, str)
            and generation_id
            and row["integrity"] == "valid"
            and isinstance(episode_count, int)
            and episode_count >= 0
            and row["document_count"] == episode_count
            and row["fts_count"] == episode_count
        ):
            backed.append(
                (
                    row["corpus_id"],
                    row["source_id"],
                    row["member_id"],
                    generation_id,
                )
            )
    return tuple(backed)


def _population(
    connection,
    backed_generations: tuple[tuple[str, str, str, str], ...],
    encoded_query: str,
) -> dict[str, int]:
    if not backed_generations:
        return {}
    parameters = tuple(
        value for generation in backed_generations for value in generation
    ) + (encoded_query,)
    rows = connection.execute(
        _POPULATION_SQL.format(
            backed_generations=_values_sql(4, len(backed_generations))
        ),
        parameters,
    ).fetchall()
    return {row["corpus_id"]: row["match_count"] for row in rows}


def _results(
    connection,
    backed_generations: tuple[tuple[str, str, str, str], ...],
    encoded_query: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not backed_generations:
        return []
    parameters = tuple(
        value for generation in backed_generations for value in generation
    ) + (encoded_query, limit)
    rows = connection.execute(
        _RESULTS_SQL.format(
            backed_generations=_values_sql(4, len(backed_generations))
        ),
        parameters,
    ).fetchall()
    return [dict(row) for row in rows]


def _corpus_is_fully_backed(
    corpus: dict[str, Any], backed: frozenset[tuple[str, str, str]]
) -> bool:
    return all(
        source["source_set_standing"] == "available"
        and all(
            member["index_standing"] == "available"
            and (corpus["corpus_id"], source["source_id"], member["member_id"])
            in backed
            for member in source["members"]
        )
        for source in corpus["sources"]
    )


def search_history(
    store: SQLiteStore,
    registry: EnrollmentRegistry,
    request: SearchRequest,
    budget: WorkBudget,
) -> dict[str, Any]:
    validated = _validated_request(request)
    if not isinstance(registry, EnrollmentRegistry):
        raise ContractError("registry must be an EnrollmentRegistry")
    scoped_registry = _scoped_registry(registry, validated.corpus_ids)
    reconciliation = reconcile_registry(store, scoped_registry, budget)
    expected_members = _expected_members(reconciliation)
    encoded_query = encode_fts5_query(validated.query)

    with store.read_transaction() as connection:
        backed_generations = _backed_generations(connection, expected_members)
        totals = _population(connection, backed_generations, encoded_query)
        total_matches = sum(totals.values())
        bounded_documents = _results(
            connection, backed_generations, encoded_query, validated.limit
        )

    backed_members = frozenset(
        generation[:3] for generation in backed_generations
    )
    reports = {
        report["corpus_id"]: report for report in reconciliation.corpus_standing
    }
    corpus_standing = []
    every_corpus_backed = True
    for corpus_id in validated.corpus_ids:
        report = reports[corpus_id]
        fully_backed = _corpus_is_fully_backed(report, backed_members)
        every_corpus_backed = every_corpus_backed and fully_backed
        corpus_standing.append(
            {
                "corpus_id": corpus_id,
                "indexed_matches": totals.get(corpus_id, 0) if fully_backed else None,
                "match_standing": "exact" if fully_backed else "unknown",
                "sources": [_public_source(source) for source in report["sources"]],
            }
        )

    results = [
        _public_result(document, validated.query)
        for document in bounded_documents
    ]
    return {
        "contract_version": CONTRACT_VERSION,
        "query": validated.query,
        "strategy": validated.strategy,
        "match_semantics": _MATCH_SEMANTICS,
        "corpus_ids_considered": list(validated.corpus_ids),
        "corpus_standing": corpus_standing,
        "returned_count": len(results),
        "total_matches": total_matches if every_corpus_backed else None,
        "total_standing": "exact" if every_corpus_backed else "unknown",
        "results": results,
    }
