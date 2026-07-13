from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llm_memory.contract import CONTRACT_VERSION, SearchRequest
from llm_memory.contract_index import CONTRACT_EPISODES, ensure_contract_index
from llm_memory.db import get_database
from llm_memory.enrollment import EnrollmentRegistry, load_registry
from llm_memory.history import open_episode, search_history
from llm_memory.lifecycle import purge_derived
from llm_memory.reconcile import WorkBudget, reconcile_registry


_RECONCILIATION_MAX_BYTES = 1_000_000
_PURGE_CLASSES = frozenset({"episodes", "reconciliation", "supersessions"})
_LIMITATIONS = (
    "The journey reports contract standing and lexical outcomes, not semantic "
    "retrieval quality.",
    "Query text, enrollment identifiers, source locators, episode references, "
    "and episode content are redacted.",
    "Serialized-byte projection is an AQL representation measurement, not "
    "storage-engine disk usage.",
    "The provider obtains result and count outcomes in one query, so standalone "
    "count latency is unavailable.",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _token(kind: str, value: str) -> str:
    return f"{kind}-{_digest(f'{kind}\0{value}')[:16]}"


def _projection(db, corpus_ids: tuple[str, ...]) -> dict[str, int]:
    result = list(
        db.aql.execute(
            """
            LET sizes = (
                FOR doc IN @@episodes
                    FILTER doc.corpus_id IN @corpus_ids
                    RETURN LENGTH(TO_STRING(UNSET(doc, "_id", "_rev")))
            )
            RETURN {
                documents: LENGTH(sizes),
                serialized_bytes: SUM(sizes)
            }
            """,
            bind_vars={
                "@episodes": CONTRACT_EPISODES,
                "corpus_ids": list(corpus_ids),
            },
        )
    )
    return result[0] if result else {"documents": 0, "serialized_bytes": 0}


def _registry_record(registry: EnrollmentRegistry) -> list[dict[str, Any]]:
    return [
        {
            "corpus_id": source.corpus_id,
            "source_id": source.source_id,
            "adapter": source.adapter,
            "enabled": source.enabled,
            "locator": str(source.locator),
        }
        for source in registry.sources
    ]


def run_journey(
    config: Path,
    query: str,
    limit: int,
    expected_ref: str | None = None,
) -> dict:
    registry = load_registry(config)
    corpus_ids = tuple(
        sorted({source.corpus_id for source in registry.sources if source.enabled})
    )
    if not corpus_ids:
        raise ValueError("journey config must contain an enabled source")

    db = get_database()
    ensure_contract_index(db)
    before = _projection(db, corpus_ids)
    now = datetime.now(UTC)
    reconciliation = reconcile_registry(
        db,
        registry,
        WorkBudget(_RECONCILIATION_MAX_BYTES, now),
    )
    after_reconciliation = _projection(db, corpus_ids)

    search_budget = WorkBudget(_RECONCILIATION_MAX_BYTES, now)
    search_started = time.perf_counter()
    search = search_history(
        db,
        registry,
        SearchRequest.create(query, corpus_ids, limit=limit),
        search_budget,
    )
    search_elapsed_ms = (time.perf_counter() - search_started) * 1_000
    after = _projection(db, corpus_ids)

    opening = None
    if expected_ref is not None:
        opening = open_episode(db, registry, expected_ref, list(corpus_ids))

    return {
        "contract_version": CONTRACT_VERSION,
        "query": query,
        "limit": limit,
        "enrollments": _registry_record(registry),
        "reconciliation": {
            "bytes_read": reconciliation.bytes_read,
            "elapsed_ms": reconciliation.elapsed_ms,
            "work_exhausted": reconciliation.work_exhausted,
            "corpus_standing": reconciliation.corpus_standing,
            "automatic_search_bytes_read": search_budget.bytes_read,
        },
        "search": search,
        "search_elapsed_ms": search_elapsed_ms,
        "opening": opening,
        "index_before": before,
        "index_after_reconciliation": after_reconciliation,
        "index_after": after,
        "purge_counts": {name: 0 for name in sorted(_PURGE_CLASSES)},
        "limitations": list(_LIMITATIONS),
    }


def _validation_age(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    validated = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return max(0.0, (now - validated).total_seconds())


def _redact_standing(search: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    standing = []
    for corpus in search["corpus_standing"]:
        corpus_token = _token("corpus", corpus["corpus_id"])
        sources = []
        for source in corpus["sources"]:
            members = []
            for member in source["members"]:
                integrity = member.get("integrity") or {}
                members.append(
                    {
                        "member": _token("member", member["member_id"]),
                        "source_standing": member["source_standing"],
                        "index_standing": member["index_standing"],
                        "freshness": member["freshness"],
                        "indexed_through": member["indexed_through"],
                        "observed_source_end": member["observed_source_end"],
                        "integrity_basis": integrity.get("basis"),
                        "validation_age_seconds": _validation_age(
                            integrity.get("validated_at"), now
                        ),
                    }
                )
            sources.append(
                {
                    "source": _token("source", source["source_id"]),
                    "adapter": source["adapter"],
                    "implementation_version": source["implementation_version"],
                    "canonicalization_version": source["canonicalization_version"],
                    "boundary_version": source["boundary_version"],
                    "source_set_standing": source["source_set_standing"],
                    "members": members,
                }
            )
        standing.append(
            {
                "corpus": corpus_token,
                "indexed_matches": corpus["indexed_matches"],
                "match_standing": corpus["match_standing"],
                "sources": sources,
            }
        )
    return standing


def _redact_opening(opening: dict[str, Any] | None) -> dict[str, Any] | None:
    if opening is None:
        return None
    redacted = {
        "episode_ref_digest": _digest(opening["episode_ref"]),
        "standing": opening["standing"],
    }
    provenance = opening.get("provenance") or {}
    if provenance.get("content_digest"):
        redacted["content_digest"] = provenance["content_digest"]
    if opening.get("replacement_ref"):
        redacted["replacement_ref_digest"] = _digest(opening["replacement_ref"])
    return redacted


def redact_journey(result: dict) -> dict:
    search = result["search"]
    now = datetime.now(UTC)
    corpus_tokens = {
        corpus["corpus_id"]: _token("corpus", corpus["corpus_id"])
        for corpus in search["corpus_standing"]
    }
    before = result["index_before"]
    after = result["index_after"]
    return {
        "contract_version": result["contract_version"],
        "query": {
            "digest": _digest(result["query"]),
            "length": len(result["query"]),
            "limit": result["limit"],
            "strategy": search["strategy"],
            "match_semantics": search["match_semantics"],
        },
        "enrollments": [
            {
                "corpus": _token("corpus", enrollment["corpus_id"]),
                "source": _token("source", enrollment["source_id"]),
                "adapter": enrollment["adapter"],
                "enabled": enrollment["enabled"],
            }
            for enrollment in result["enrollments"]
        ],
        "standing": _redact_standing(search, now),
        "counts": {
            "returned": search["returned_count"],
            "total_matches": search["total_matches"],
            "total_standing": search["total_standing"],
        },
        "results": [
            {
                "rank": rank,
                "episode_ref_digest": _digest(item["episode_ref"]),
                "corpus": corpus_tokens[item["corpus_id"]],
            }
            for rank, item in enumerate(search["results"], start=1)
        ],
        "opening": _redact_opening(result["opening"]),
        "reconciliation": {
            "bytes_read": result["reconciliation"]["bytes_read"],
            "elapsed_ms": result["reconciliation"]["elapsed_ms"],
            "work_exhausted": result["reconciliation"]["work_exhausted"],
            "automatic_search_bytes_read": result["reconciliation"][
                "automatic_search_bytes_read"
            ],
        },
        "timing": {
            "search_with_count_elapsed_ms": result["search_elapsed_ms"],
            "count_elapsed_ms": None,
            "count_timing_standing": "combined_with_search",
        },
        "index_growth": {
            "documents": after["documents"] - before["documents"],
            "serialized_bytes": after["serialized_bytes"]
            - before["serialized_bytes"],
        },
        "purge_counts": dict(result["purge_counts"]),
        "limitations": list(result["limitations"]),
    }


def _test_corpus_ids(config: Path) -> list[str]:
    registry = load_registry(config)
    corpus_ids = sorted({source.corpus_id for source in registry.sources})
    if not corpus_ids or any(
        not corpus_id.startswith("test-") for corpus_id in corpus_ids
    ):
        raise ValueError(
            "purge is restricted to corpus identifiers beginning with 'test-'"
        )
    return corpus_ids


def _purge_test_corpora(config: Path) -> dict[str, int]:
    corpus_ids = _test_corpus_ids(config)
    db = get_database()
    counts = {name: 0 for name in sorted(_PURGE_CLASSES)}
    for corpus_id in corpus_ids:
        removed = purge_derived(db, corpus_id, classes=_PURGE_CLASSES)
        for name, count in removed.items():
            counts[name] += count
    return counts


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a redacted episodic contract journey"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-ref")
    parser.add_argument("--purge-test-corpus", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.purge_test_corpus:
        _test_corpus_ids(arguments.config)
    raw = run_journey(
        arguments.config,
        arguments.query,
        arguments.limit,
        arguments.expected_ref,
    )
    if arguments.purge_test_corpus:
        raw["purge_counts"] = _purge_test_corpora(arguments.config)
    _atomic_write_json(arguments.output, redact_journey(raw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
