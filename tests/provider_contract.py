from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Mapping

import pytest

from llm_memory.adapters import get_adapter
from llm_memory.contract import ContractError, EpisodeReference, SearchRequest
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.opening import open_episode
from llm_memory.provider import EpisodicProvider, PurgeScope
from llm_memory.reconcile import WorkBudget


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class SyntheticSourceFixture:
    registry: EnrollmentRegistry
    path: Path
    original_bytes: bytes
    original_files: Mapping[Path, bytes]
    corpus_id: str
    sentinel_corpus_id: str

    def source(self, source_id: str) -> SourceEnrollment:
        return next(
            source for source in self.registry.sources if source.source_id == source_id
        )


def _registry_with(
    registry: EnrollmentRegistry,
    source_id: str,
    **changes,
) -> EnrollmentRegistry:
    return EnrollmentRegistry(
        tuple(
            replace(source, **changes) if source.source_id == source_id else source
            for source in registry.sources
        )
    )


def _registry_without(
    registry: EnrollmentRegistry, source_id: str
) -> EnrollmentRegistry:
    return EnrollmentRegistry(
        tuple(source for source in registry.sources if source.source_id != source_id)
    )


def _episode_refs(source: SourceEnrollment) -> tuple[str, ...]:
    adapter = get_adapter(source.adapter)
    member = adapter.members(source)[0]
    return tuple(
        episode.identity.episode_ref for episode in adapter.scan(source, member).episodes
    )


def _member_reports(report) -> tuple[dict, ...]:
    return tuple(
        member
        for corpus in report.corpus_standing
        for source in corpus["sources"]
        for member in source["members"]
    )


def _assert_files(
    fixture: SyntheticSourceFixture,
    *,
    primary_bytes: bytes | None = None,
) -> None:
    expected = dict(fixture.original_files)
    if primary_bytes is not None:
        expected[fixture.path] = primary_bytes
    assert {path: path.read_bytes() for path in expected} == expected


def _search(
    provider: EpisodicProvider,
    registry: EnrollmentRegistry,
    corpus_id: str,
    strategy: str,
    query: str,
    *,
    limit: int = 10,
    now: datetime = NOW,
) -> dict[str, object]:
    return provider.search(
        registry,
        SearchRequest.create(query, [corpus_id], limit=limit, strategy=strategy),
        WorkBudget(1_000_000, now),
    )


def assert_portable_provider_contract(
    provider: EpisodicProvider,
    synthetic_source: SyntheticSourceFixture,
    *,
    strategy: str,
    foreign_strategy: str,
) -> bytes:
    registry = synthetic_source.registry
    corpus_id = synthetic_source.corpus_id
    primary = synthetic_source.source("primary")
    expected_primary_refs = frozenset(_episode_refs(primary))
    expected_search_refs = frozenset(
        episode_ref
        for source in registry.sources_for(corpus_id)
        for episode_ref in _episode_refs(source)
    )
    expected_secondary_refs = frozenset(
        _episode_refs(synthetic_source.source("secondary"))
    )

    capabilities = provider.capabilities()
    assert capabilities["strategies"] == [strategy]
    assert capabilities["retrieval_basis"]["strategies"] == (strategy,)
    assert provider.ensure()["index_standing"] == "available"

    bounded = provider.reconcile(registry, WorkBudget(1, NOW))
    assert bounded.work_exhausted is True
    assert any(
        member["freshness"] != "current" for member in _member_reports(bounded)
    )
    for _ in range(32):
        resumed = provider.reconcile(registry, WorkBudget(1, NOW))
        enabled_members = _member_reports(resumed)
        if enabled_members and all(
            member["freshness"] == "current"
            and member["index_standing"] == "available"
            for member in enabled_members
        ):
            break
    else:
        raise AssertionError("bounded reconciliation did not resume to current")

    first = _search(
        provider,
        registry,
        corpus_id,
        strategy,
        "portable decision",
        limit=2,
    )
    second = _search(
        provider,
        registry,
        corpus_id,
        strategy,
        "portable decision",
        limit=2,
    )
    assert first["returned_count"] <= 2
    assert first["returned_count"] == len(first["results"])
    assert first["total_standing"] in {"exact", "unknown"}
    if first["total_standing"] == "exact":
        assert isinstance(first["total_matches"], int)
    else:
        assert first["total_matches"] is None
    assert first["results"] == second["results"]
    assert {
        result["episode_ref"] for result in first["results"]
    } <= expected_search_refs
    assert first["corpus_standing"][0]["corpus_id"] == corpus_id
    sources = {
        source["source_id"]: source
        for source in first["corpus_standing"][0]["sources"]
    }
    assert set(sources) == {"empty", "primary", "secondary"}
    assert sources["empty"]["source_set_standing"] == "available"
    assert sources["empty"]["members"][0]["index_standing"] == "available"
    assert all(source["members"] for source in sources.values())
    disabled_declaration = _search(
        provider,
        registry,
        corpus_id,
        strategy,
        "forbidden",
    )
    assert disabled_declaration["returned_count"] == 0
    secondary = _search(
        provider,
        registry,
        corpus_id,
        strategy,
        "secondary",
    )
    assert secondary["returned_count"] == 1
    assert {
        result["episode_ref"] for result in secondary["results"]
    } == expected_secondary_refs
    assert all(
        EpisodeReference.parse(result["episode_ref"]).source_id == "secondary"
        for result in secondary["results"]
    )

    primary_result = _search(
        provider,
        registry,
        corpus_id,
        strategy,
        "outcome",
        limit=1,
    )["results"][0]
    reference = EpisodeReference.parse(primary_result["episode_ref"])
    assert reference.corpus_id == corpus_id
    assert reference.source_id == "primary"
    assert primary_result["episode_ref"] in expected_primary_refs
    opened = open_episode(
        registry,
        primary_result["episode_ref"],
        [corpus_id],
        provider.resolve_supersession,
    )
    assert opened["standing"] == "available"
    assert opened["provenance"]["content_digest"] == reference.content_digest
    _assert_files(synthetic_source)

    with pytest.raises(ContractError, match="unsupported strategy"):
        _search(
            provider,
            registry,
            corpus_id,
            foreign_strategy,
            "portable decision",
        )

    records = [
        json.loads(line) for line in synthetic_source.path.read_text().splitlines()
    ]
    # Keep the source length stable so this is a prefix rewrite, not an append.
    records[0]["response_text"] = "rewrite authority outcome 1"
    rewritten_bytes = b"".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
        for record in records
    )
    synthetic_source.path.write_bytes(rewritten_bytes)
    old_ref = next(
        episode_ref
        for episode_ref in expected_primary_refs
        if EpisodeReference.parse(episode_ref).event_token == "1"
    )
    new_ref = _episode_refs(primary)[0]
    assert new_ref != old_ref
    later = NOW + timedelta(seconds=2)
    for _ in range(8):
        provider.reconcile(registry, WorkBudget(1_000_000, later))
        if provider.resolve_supersession(primary, old_ref) == new_ref:
            break
    else:
        raise AssertionError("rewrite supersession was not recorded")
    superseded = open_episode(
        registry, old_ref, [corpus_id], provider.resolve_supersession
    )
    assert superseded == {
        "contract_version": 1,
        "episode_ref": old_ref,
        "standing": "superseded",
        "replacement_ref": new_ref,
    }
    _assert_files(synthetic_source, primary_bytes=rewritten_bytes)

    primary_scope = PurgeScope(corpus_id=corpus_id, source_id="primary")
    retained_before = provider.measure(primary_scope)
    assert retained_before.standing == "available"
    retained_counts = {
        key: retained_before.observations[key]
        for key in (
            "episode_documents",
            "source_state_documents",
            "supersession_documents",
        )
    }
    assert retained_counts == {
        "episode_documents": 3,
        "source_state_documents": 1,
        "supersession_documents": 1,
    }

    disabled_registry = _registry_with(registry, "primary", enabled=False)
    disabled = _search(
        provider,
        disabled_registry,
        corpus_id,
        strategy,
        "outcome",
        now=later,
    )
    assert disabled["returned_count"] == 0
    with pytest.raises(ContractError, match="not uniquely enrolled and enabled"):
        open_episode(
            disabled_registry,
            new_ref,
            [corpus_id],
            provider.resolve_supersession,
        )

    reenrolled = _search(
        provider,
        registry,
        corpus_id,
        strategy,
        "outcome",
        now=later,
    )
    assert reenrolled["returned_count"] >= 1

    unenrolled_registry = _registry_without(registry, "primary")
    unenrolled = _search(
        provider,
        unenrolled_registry,
        corpus_id,
        strategy,
        "outcome",
        now=later,
    )
    assert unenrolled["returned_count"] == 0
    with pytest.raises(ContractError, match="not uniquely enrolled and enabled"):
        open_episode(
            unenrolled_registry,
            new_ref,
            [corpus_id],
            provider.resolve_supersession,
        )
    retained_after = provider.measure(primary_scope)
    assert {
        key: retained_after.observations[key] for key in retained_counts
    } == retained_counts

    sentinel_scope = PurgeScope(
        corpus_id=synthetic_source.sentinel_corpus_id,
        source_id="sentinel",
    )
    sentinel_before = provider.measure(sentinel_scope)
    sentinel_counts = {
        key: sentinel_before.observations[key] for key in retained_counts
    }
    assert sentinel_counts == {
        "episode_documents": 1,
        "source_state_documents": 1,
        "supersession_documents": 0,
    }
    purged = provider.purge(
        primary_scope, frozenset({"episodes", "reconciliation"})
    )
    assert purged == {"episodes": 3, "reconciliation": 1}
    primary_after_purge = provider.measure(primary_scope)
    assert primary_after_purge.observations["episode_documents"] == 0
    assert primary_after_purge.observations["source_state_documents"] == 0
    assert primary_after_purge.observations["supersession_documents"] == 1
    sentinel_after = provider.measure(sentinel_scope)
    assert {
        key: sentinel_after.observations[key] for key in sentinel_counts
    } == sentinel_counts

    rebuilt = _search(
        provider,
        registry,
        corpus_id,
        strategy,
        "outcome",
        now=later,
    )
    assert rebuilt["returned_count"] >= 1
    rebuilt_measurement = provider.measure(primary_scope)
    assert {
        key: rebuilt_measurement.observations[key] for key in retained_counts
    } == retained_counts
    _assert_files(synthetic_source, primary_bytes=rewritten_bytes)
    return rewritten_bytes
