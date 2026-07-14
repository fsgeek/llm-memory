from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evaluation.stage2a_provider_experiment import (
    PublicReportError,
    Stage2AExperiment,
    run_stage2a,
    write_report_atomic,
)
from llm_memory.contract import SearchRequest
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.provider import ProviderMeasurement, ProviderUnavailable
from llm_memory.reconcile import ReconcileReport


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
ARANGO_STRATEGY = "lexical_bm25_text_en_v1"
SQLITE_STRATEGY = "lexical_bm25_fts5_porter_unicode61_v1"


class FakeProvider:
    def __init__(self, name: str, *, total_standing: str = "exact") -> None:
        self.name = name
        self.total_standing = total_standing
        self.fail_operation: str | None = None
        self.calls: list[tuple[object, ...]] = []

    @property
    def strategy(self) -> str:
        return ARANGO_STRATEGY if self.name == "arango" else SQLITE_STRATEGY

    def _maybe_fail(self, operation: str) -> None:
        if self.fail_operation == operation:
            raise ProviderUnavailable("offline-private-provider-error")

    def capabilities(self) -> dict[str, object]:
        self.calls.append(("capabilities",))
        self._maybe_fail("capabilities")
        sqlite = self.name == "sqlite"
        return {
            "contract_versions": [1],
            "strategies": [self.strategy],
            "supports_facets": False,
            "supports_continuation": False,
            "max_limit": 100,
            "retrieval_basis": {
                "provider": self.name,
                "implementation_version": "1",
                "strategies": (self.strategy,),
                "analyzer": (
                    "porter unicode61 remove_diacritics 2" if sqlite else "text_en"
                ),
                "indexed_fields": ("user_message", "response", "state_text"),
                "match_semantics": (
                    "analyzed_any_segment_phrase" if sqlite else "analyzed_any_token"
                ),
                "score_ordering": (
                    "normalized_desc_episode_ref_asc"
                    if sqlite
                    else "higher_is_better"
                ),
                "raw_score_polarity": (
                    "lower_is_better" if sqlite else "higher_is_better"
                ),
            },
        }

    def ensure(self) -> dict[str, object]:
        self.calls.append(("ensure",))
        self._maybe_fail("ensure")
        return {"provider": self.name, "index_standing": "available"}

    def reconcile(self, registry, budget) -> ReconcileReport:
        self.calls.append(("reconcile", registry))
        self._maybe_fail("reconcile")
        budget.charge(31)
        return ReconcileReport((), 31, 0.25, False)

    def search(self, registry, request, budget) -> dict[str, object]:
        self.calls.append(("search", registry, request))
        self._maybe_fail("search")
        assert request.strategy == self.strategy
        budget.charge(7)
        exact = self.total_standing == "exact"
        return {
            "contract_version": 1,
            "query": request.query,
            "strategy": request.strategy,
            "match_semantics": "private-match-semantics-from-response",
            "corpus_ids_considered": list(request.corpus_ids),
            "corpus_standing": [],
            "returned_count": 1,
            "total_matches": 2 if exact else None,
            "total_standing": self.total_standing,
            "results": [
                {
                    "episode_ref": "private-raw-episode-reference",
                    "snippet": "private-result-snippet",
                    "score": 42.0,
                }
            ],
        }

    def measure(self, scope) -> ProviderMeasurement:
        self.calls.append(("measure", scope))
        self._maybe_fail("measure")
        observations: dict[str, int | float | str | None] = {
            "episode_documents": 2,
            "source_state_documents": 1,
            "supersession_documents": 0,
        }
        if self.name == "sqlite":
            observations |= {
                "episode_fts_rows": 2,
                "database_bytes": 123,
                "database_stat_standing": "available",
                "wal_bytes": 7,
                "wal_stat_standing": "available",
                "shm_bytes": 4,
                "shm_stat_standing": "available",
                "query_standing": "available",
                "fts_representation": "self_contained_duplicate",
            }
        return ProviderMeasurement(self.name, "available", observations)

    def purge(self, *args, **kwargs):
        raise AssertionError("synthetic report runner must not purge injected providers")

    def remove_all(self):
        raise AssertionError(
            "synthetic report runner must not remove injected provider state"
        )


@pytest.fixture
def synthetic_experiment(tmp_path: Path) -> Stage2AExperiment:
    source_prose = "private synthetic source prose"
    source_path = tmp_path / "private-source.jsonl"
    source_path.write_text(source_prose, encoding="utf-8")
    corpus_id = "private-corpus-token"
    source_id = "private-source-token"
    registry = EnrollmentRegistry(
        (
            SourceEnrollment(
                corpus_id=corpus_id,
                source_id=source_id,
                adapter="taste_open_jsonl",
                boundary_version=1,
                canonicalization_version=1,
                locator=source_path,
                enabled=True,
                full_validation_max_age_seconds=3600,
            ),
        )
    )
    query = "private frozen query text"
    request = SearchRequest.create(query, [corpus_id], strategy=ARANGO_STRATEGY)
    return Stage2AExperiment(
        arango=FakeProvider("arango"),
        sqlite=FakeProvider("sqlite", total_standing="unknown"),
        registry=registry,
        requests=(request,),
        private_values=(
            source_prose,
            query,
            corpus_id,
            source_id,
            str(source_path),
            "private-raw-episode-reference",
            "private-result-snippet",
            "offline-private-provider-error",
        ),
    )


def test_report_excludes_content_and_identifiers(
    synthetic_experiment: Stage2AExperiment,
) -> None:
    report = run_stage2a(synthetic_experiment)

    encoded = json.dumps(report, sort_keys=True)
    for forbidden in synthetic_experiment.private_values:
        assert forbidden not in encoded
    assert set(report["providers"]) == {"arango", "sqlite"}
    assert "aggregate_score" not in encoded
    assert "winner" not in encoded
    assert report["source_basis"] == "synthetic_only"
    assert report["decision"] == "phase_a_checkpoint_only"
    assert report["phase_a_scope"] == {
        "mechanics_only": True,
        "rationale_usefulness_proven": False,
        "phase_b_authorized": False,
    }


def test_provider_failure_is_not_fallback(
    synthetic_experiment: Stage2AExperiment,
) -> None:
    synthetic_experiment.arango.fail_operation = "search"

    report = run_stage2a(synthetic_experiment)

    assert report["providers"]["arango"]["standing"] == "unavailable"
    assert report["providers"]["arango"]["lock_or_outage"] == {
        "standing": "observed",
        "operation": "search",
    }
    assert report["providers"]["sqlite"]["standing"] == "available"
    assert any(call[0] == "search" for call in synthetic_experiment.sqlite.calls)


def test_experiment_requires_separately_constructed_providers(
    synthetic_experiment: Stage2AExperiment,
) -> None:
    shared = synthetic_experiment.arango

    with pytest.raises(ValueError, match="separately constructed"):
        Stage2AExperiment(
            arango=shared,
            sqlite=shared,
            registry=synthetic_experiment.registry,
            requests=synthetic_experiment.requests,
            private_values=synthetic_experiment.private_values,
        )


def test_malformed_measurement_does_not_suppress_other_provider(
    synthetic_experiment: Stage2AExperiment,
) -> None:
    synthetic_experiment.arango.measure = lambda scope: ProviderMeasurement(
        "arango", "available", None
    )

    report = run_stage2a(synthetic_experiment)

    assert report["providers"]["arango"]["standing"] == "unavailable"
    assert report["providers"]["arango"]["lock_or_outage"] == {
        "standing": "not_observed",
        "operation": None,
    }
    assert report["providers"]["sqlite"]["standing"] == "available"


def test_provider_records_retain_independent_measurement_basis(
    synthetic_experiment: Stage2AExperiment,
) -> None:
    report = run_stage2a(synthetic_experiment)
    arango = report["providers"]["arango"]
    sqlite = report["providers"]["sqlite"]

    required = {
        "standing",
        "retrieval_basis",
        "schema_readiness",
        "source_bytes",
        "database_work",
        "elapsed",
        "search_totals",
        "derived_state_counts",
        "derived_physical_bytes",
        "lock_or_outage",
        "purge",
        "rebuild",
        "full_removal",
    }
    assert set(arango) == required
    assert set(sqlite) == required
    assert arango["retrieval_basis"] == {
        "provider": "arango",
        "implementation_version": "1",
        "strategy": ARANGO_STRATEGY,
        "analyzer": "text_en",
        "indexed_fields": ["user_message", "response", "state_text"],
        "match_semantics": "analyzed_any_token",
        "public_score_polarity": "higher_is_better",
        "raw_score_polarity": "higher_is_better",
    }
    assert sqlite["retrieval_basis"]["strategy"] == SQLITE_STRATEGY
    assert sqlite["retrieval_basis"]["public_score_polarity"] == (
        "normalized_desc_episode_ref_asc"
    )
    assert sqlite["retrieval_basis"]["raw_score_polarity"] == "lower_is_better"
    assert arango["search_totals"] == [
        {
            "query_token": "query-001",
            "returned_count": 1,
            "total_matches": 2,
            "total_standing": "exact",
        }
    ]
    assert sqlite["search_totals"] == [
        {
            "query_token": "query-001",
            "returned_count": 1,
            "total_matches": None,
            "total_standing": "unknown",
        }
    ]
    assert arango["source_bytes"] == {
        "standing": "exact",
        "basis": "work_budget_charges",
        "bytes": 38,
    }
    assert arango["database_work"] == {
        "standing": "not_measured",
        "basis": "provider_work_units_not_exposed",
    }
    assert arango["derived_physical_bytes"] == {
        "standing": "unavailable",
        "basis": "provider_did_not_report_physical_bytes",
        "total_bytes": None,
        "artifacts": {},
    }
    assert sqlite["derived_physical_bytes"] == {
        "standing": "available",
        "basis": "provider_reported_file_stats",
        "total_bytes": 134,
        "artifacts": {
            "database": {"standing": "available", "bytes": 123},
            "wal": {"standing": "available", "bytes": 7},
            "shm": {"standing": "available", "bytes": 4},
        },
    }
    assert sqlite["derived_state_counts"] == {
        "standing": "available",
        "basis": "provider_measurement",
        "counts": {
            "episode_documents": 2,
            "episode_fts_rows": 2,
            "source_state_documents": 1,
            "supersession_documents": 0,
        },
    }
    assert arango["elapsed"]["basis"] == "monotonic_inclusive"
    assert arango["elapsed"]["standing"] == "measured"
    assert isinstance(arango["elapsed"]["milliseconds"], float)


def test_runner_never_destructively_operates_on_injected_providers(
    synthetic_experiment: Stage2AExperiment,
) -> None:
    report = run_stage2a(synthetic_experiment)

    for name, provider in (
        ("arango", synthetic_experiment.arango),
        ("sqlite", synthetic_experiment.sqlite),
    ):
        assert not {"purge", "remove_all"} & {call[0] for call in provider.calls}
        assert report["providers"][name]["purge"] == {
            "standing": "unavailable",
            "basis": "destructive_evidence_not_collected",
            "counts": {
                "episodes": None,
                "reconciliation": None,
                "supersessions": None,
            },
        }
        assert report["providers"][name]["rebuild"] == {
            "standing": "unavailable",
            "basis": "purge_not_exercised",
        }
    assert report["providers"]["arango"]["full_removal"] == {
        "standing": "unavailable",
        "basis": "shared_arango_remove_all_prohibited",
        "residual_count": None,
        "declared_losses": ["retained_supersession_observations"],
    }
    assert report["providers"]["sqlite"]["full_removal"] == {
        "standing": "unavailable",
        "basis": "owned_disposable_provider_not_declared",
        "residual_count": None,
        "declared_losses": [
            "retained_supersession_observations",
            "non_reproducible_evaluation_state",
        ],
    }


def test_report_validation_precedes_temporary_public_write(
    synthetic_experiment: Stage2AExperiment, tmp_path: Path, monkeypatch
) -> None:
    report = run_stage2a(synthetic_experiment)
    report["providers"]["arango"]["unexpected"] = "private synthetic source prose"
    target = tmp_path / "report.json"

    def fail_if_called(*args, **kwargs):
        raise AssertionError("temporary file created before public validation")

    monkeypatch.setattr(
        "evaluation.stage2a_provider_experiment.tempfile.mkstemp", fail_if_called
    )

    with pytest.raises(PublicReportError, match="provider record keys"):
        write_report_atomic(target, report)

    assert not target.exists()
    assert list(tmp_path.glob(".report.json.*.tmp")) == []


def test_atomic_replace_failure_preserves_target_and_removes_temporary(
    synthetic_experiment: Stage2AExperiment, tmp_path: Path, monkeypatch
) -> None:
    report = run_stage2a(synthetic_experiment)
    target = tmp_path / "report.json"
    original = b"existing public report\n"
    target.write_bytes(original)

    def fail_replace(source, destination):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(
        "evaluation.stage2a_provider_experiment.os.replace", fail_replace
    )

    with pytest.raises(OSError, match="synthetic replace failure"):
        write_report_atomic(target, report)

    assert target.read_bytes() == original
    assert list(tmp_path.glob(".report.json.*.tmp")) == []


def test_atomic_write_fsyncs_before_replace(
    synthetic_experiment: Stage2AExperiment, tmp_path: Path, monkeypatch
) -> None:
    report = run_stage2a(synthetic_experiment)
    target = tmp_path / "report.json"
    events: list[str] = []
    original_fsync = os.fsync
    original_replace = os.replace

    def observed_fsync(descriptor: int) -> None:
        events.append("fsync")
        original_fsync(descriptor)

    def observed_replace(source, destination) -> None:
        events.append("replace")
        original_replace(source, destination)

    monkeypatch.setattr(
        "evaluation.stage2a_provider_experiment.os.fsync", observed_fsync
    )
    monkeypatch.setattr(
        "evaluation.stage2a_provider_experiment.os.replace", observed_replace
    )

    write_report_atomic(target, report)

    assert events == ["fsync", "replace"]
    assert json.loads(target.read_text(encoding="utf-8")) == report
    assert target.read_bytes().endswith(b"\n")
