from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Mapping

from llm_memory.contract import CONTRACT_VERSION, SearchRequest
from llm_memory.enrollment import EnrollmentRegistry
from llm_memory.provider import (
    EpisodicProvider,
    ProviderMeasurement,
    ProviderUnavailable,
    PurgeScope,
)
from llm_memory.reconcile import WorkBudget


_WORK_BUDGET_BYTES = 1_000_000
_PROVIDER_NAMES = ("arango", "sqlite")
_COUNT_FIELDS = frozenset(
    {
        "episode_documents",
        "episode_fts_rows",
        "source_state_documents",
        "supersession_documents",
    }
)
_PROVIDER_COUNT_FIELDS = {
    "arango": frozenset(
        {
            "episode_documents",
            "source_state_documents",
            "supersession_documents",
        }
    ),
    "sqlite": _COUNT_FIELDS,
}
_SQLITE_PHYSICAL_FIELDS = frozenset(
    f"{artifact}_{suffix}"
    for artifact in ("database", "wal", "shm")
    for suffix in ("bytes", "stat_standing")
)
_DESCRIPTORS: dict[str, dict[str, object]] = {
    "arango": {
        "provider": "arango",
        "implementation_version": "1",
        "strategy": "lexical_bm25_text_en_v1",
        "analyzer": "text_en",
        "indexed_fields": ["user_message", "response", "state_text"],
        "match_semantics": "analyzed_any_token",
        "public_score_polarity": "higher_is_better",
        "raw_score_polarity": "higher_is_better",
    },
    "sqlite": {
        "provider": "sqlite",
        "implementation_version": "1",
        "strategy": "lexical_bm25_fts5_porter_unicode61_v1",
        "analyzer": "porter unicode61 remove_diacritics 2",
        "indexed_fields": ["user_message", "response", "state_text"],
        "match_semantics": "analyzed_any_segment_phrase",
        "public_score_polarity": "normalized_desc_episode_ref_asc",
        "raw_score_polarity": "lower_is_better",
    },
}
_DECLARED_LOSSES = {
    "arango": ["retained_supersession_observations"],
    "sqlite": [
        "retained_supersession_observations",
        "non_reproducible_evaluation_state",
    ],
}
_PROVIDER_RECORD_KEYS = frozenset(
    {
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
)


class PublicReportError(ValueError):
    """A value cannot cross the content-free public report boundary."""


@dataclass(frozen=True)
class Stage2AExperiment:
    arango: EpisodicProvider
    sqlite: EpisodicProvider
    registry: EnrollmentRegistry
    requests: tuple[SearchRequest, ...]
    private_values: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.arango is self.sqlite:
            raise ValueError("providers must be separately constructed")


def _empty_retrieval_basis(name: str) -> dict[str, object]:
    return {
        "provider": name,
        "implementation_version": None,
        "strategy": None,
        "analyzer": None,
        "indexed_fields": [],
        "match_semantics": None,
        "public_score_polarity": None,
        "raw_score_polarity": None,
    }


def _retrieval_basis(name: str, capabilities: object) -> dict[str, object]:
    if not isinstance(capabilities, Mapping):
        raise ValueError("capabilities must be a mapping")
    raw = capabilities.get("retrieval_basis")
    if not isinstance(raw, Mapping):
        raise ValueError("retrieval basis must be a mapping")
    strategies = raw.get("strategies")
    if not isinstance(strategies, (tuple, list)) or len(strategies) != 1:
        raise ValueError("provider must declare exactly one strategy")
    basis = {
        "provider": raw.get("provider"),
        "implementation_version": raw.get("implementation_version"),
        "strategy": strategies[0],
        "analyzer": raw.get("analyzer"),
        "indexed_fields": list(raw.get("indexed_fields", ())),
        "match_semantics": raw.get("match_semantics"),
        "public_score_polarity": raw.get("score_ordering"),
        "raw_score_polarity": raw.get("raw_score_polarity"),
    }
    if basis != _DESCRIPTORS[name]:
        raise ValueError("provider retrieval basis differs from frozen Phase A basis")
    return basis


def _search_total(token: str, response: object) -> dict[str, object]:
    if not isinstance(response, Mapping):
        raise ValueError("search response must be a mapping")
    returned_count = response.get("returned_count")
    total_matches = response.get("total_matches")
    total_standing = response.get("total_standing")
    if (
        isinstance(returned_count, bool)
        or not isinstance(returned_count, int)
        or returned_count < 0
    ):
        raise ValueError("returned count must be a non-negative integer")
    if total_standing == "exact":
        if (
            isinstance(total_matches, bool)
            or not isinstance(total_matches, int)
            or total_matches < returned_count
        ):
            raise ValueError("exact total must cover the returned count")
    elif total_standing == "unknown":
        if total_matches is not None:
            raise ValueError("unknown total must not include a count")
    else:
        raise ValueError("total standing must be exact or unknown")
    return {
        "query_token": token,
        "returned_count": returned_count,
        "total_matches": total_matches,
        "total_standing": total_standing,
    }


def _unavailable_search_total(token: str) -> dict[str, object]:
    return {
        "query_token": token,
        "returned_count": None,
        "total_matches": None,
        "total_standing": "unavailable",
    }


def _valid_count(value: object, *, nullable: bool) -> bool:
    return (value is None and nullable) or (
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
    )


def _validated_measurement(
    name: str, observed: object
) -> ProviderMeasurement:
    if (
        not isinstance(observed, ProviderMeasurement)
        or observed.provider != name
        or observed.standing not in {"available", "unavailable"}
        or not isinstance(observed.observations, dict)
    ):
        raise ValueError("invalid provider measurement")
    observations = observed.observations
    count_fields = _PROVIDER_COUNT_FIELDS[name]
    nullable_counts = observed.standing == "unavailable"
    if not count_fields <= observations.keys() or not all(
        _valid_count(observations[field], nullable=nullable_counts)
        for field in count_fields
    ):
        raise ValueError("invalid provider measurement counts")

    if name == "arango":
        if set(observations) != count_fields:
            raise ValueError("invalid Arango measurement shape")
        return observed

    expected = count_fields | _SQLITE_PHYSICAL_FIELDS | {
        "scope",
        "corpus_id",
        "source_id",
        "query_standing",
        "fts_representation",
    }
    if set(observations) != expected or (
        observations["scope"] != "global"
        or observations["corpus_id"] is not None
        or observations["source_id"] is not None
        or observations["query_standing"] not in {"available", "unavailable"}
        or observations["fts_representation"] != "self_contained_duplicate"
    ):
        raise ValueError("invalid SQLite measurement shape")
    if not all(
        _valid_count(observations[f"{artifact}_bytes"], nullable=False)
        and observations[f"{artifact}_stat_standing"]
        in {"available", "absent", "unavailable"}
        for artifact in ("database", "wal", "shm")
    ):
        raise ValueError("invalid SQLite physical measurement")
    query_available = observations["query_standing"] == "available"
    if (
        query_available
        and not all(
            _valid_count(observations[field], nullable=False)
            for field in count_fields
        )
    ) or (
        not query_available
        and not all(observations[field] is None for field in count_fields)
    ):
        raise ValueError("SQLite count standing does not match count evidence")
    expected_standing = (
        "available"
        if observations["database_stat_standing"] == "available"
        and query_available
        else "unavailable"
    )
    if observed.standing != expected_standing:
        raise ValueError("SQLite measurement standing is inconsistent")
    return observed


def _state_counts(measurement: ProviderMeasurement | None) -> dict[str, object]:
    if measurement is None:
        return {
            "standing": "unavailable",
            "basis": "provider_measurement",
            "counts": {},
        }
    counts = {
        key: value
        for key, value in measurement.observations.items()
        if key in _COUNT_FIELDS and (value is None or isinstance(value, int))
    }
    return {
        "standing": measurement.standing,
        "basis": "provider_measurement",
        "counts": counts,
    }


def _physical_bytes(measurement: ProviderMeasurement | None) -> dict[str, object]:
    if measurement is None:
        return {
            "standing": "unavailable",
            "basis": "provider_did_not_report_physical_bytes",
            "total_bytes": None,
            "artifacts": {},
        }
    artifacts = {}
    for artifact in ("database", "wal", "shm"):
        byte_count = measurement.observations.get(f"{artifact}_bytes")
        standing = measurement.observations.get(f"{artifact}_stat_standing")
        if isinstance(byte_count, int) and not isinstance(byte_count, bool) and isinstance(
            standing, str
        ):
            artifacts[artifact] = {"standing": standing, "bytes": byte_count}
    if not artifacts or any(
        artifact["standing"] == "unavailable" for artifact in artifacts.values()
    ):
        return {
            "standing": "unavailable",
            "basis": "provider_did_not_report_physical_bytes",
            "total_bytes": None,
            "artifacts": {},
        }
    available = set(artifacts) == {"database", "wal", "shm"} and all(
        artifact["standing"] in {"available", "absent"}
        for artifact in artifacts.values()
    )
    return {
        "standing": "available" if available else "unavailable",
        "basis": "provider_reported_file_stats",
        "total_bytes": (
            sum(artifact["bytes"] for artifact in artifacts.values())
            if available
            else None
        ),
        "artifacts": artifacts,
    }


def _run_provider(
    name: str, provider: EpisodicProvider, experiment: Stage2AExperiment
) -> dict[str, object]:
    started = perf_counter()
    failures: list[str] = []
    outages: list[str] = []
    basis = _empty_retrieval_basis(name)
    schema_standing = "unavailable"
    source_bytes = 0
    search_totals = []
    measurement: ProviderMeasurement | None = None

    try:
        basis = _retrieval_basis(name, provider.capabilities())
    except Exception as exc:
        failures.append("capabilities")
        if isinstance(exc, ProviderUnavailable):
            outages.append("capabilities")

    try:
        readiness = provider.ensure()
        if not isinstance(readiness, Mapping) or readiness.get("index_standing") not in {
            "available",
            "rebuilding",
            "unavailable",
        }:
            raise ValueError("invalid schema readiness")
        schema_standing = str(readiness["index_standing"])
        if schema_standing != "available":
            failures.append("ensure")
    except Exception as exc:
        failures.append("ensure")
        if isinstance(exc, ProviderUnavailable):
            outages.append("ensure")

    reconcile_budget = WorkBudget(_WORK_BUDGET_BYTES, datetime.now(UTC))
    try:
        provider.reconcile(experiment.registry, reconcile_budget)
    except Exception as exc:
        failures.append("reconcile")
        if isinstance(exc, ProviderUnavailable):
            outages.append("reconcile")
    finally:
        source_bytes += reconcile_budget.bytes_read

    strategy = basis["strategy"]
    for index, request in enumerate(experiment.requests, start=1):
        token = f"query-{index:03d}"
        if not isinstance(strategy, str):
            search_totals.append(_unavailable_search_total(token))
            continue
        search_budget = WorkBudget(_WORK_BUDGET_BYTES, datetime.now(UTC))
        try:
            provider_request = SearchRequest.create(
                request.query,
                request.corpus_ids,
                limit=request.limit,
                strategy=strategy,
                contract_version=request.contract_version,
            )
            response = provider.search(
                experiment.registry, provider_request, search_budget
            )
            search_totals.append(_search_total(token, response))
        except Exception as exc:
            failures.append("search")
            if isinstance(exc, ProviderUnavailable):
                outages.append("search")
            search_totals.append(_unavailable_search_total(token))
        finally:
            source_bytes += search_budget.bytes_read

    try:
        measurement = _validated_measurement(name, provider.measure(PurgeScope()))
        if measurement.standing != "available":
            failures.append("measure")
    except Exception as exc:
        failures.append("measure")
        if isinstance(exc, ProviderUnavailable):
            outages.append("measure")

    first_outage = outages[0] if outages else None
    elapsed_ms = round((perf_counter() - started) * 1000, 3)
    return {
        "standing": "unavailable" if failures else "available",
        "retrieval_basis": basis,
        "schema_readiness": {"standing": schema_standing},
        "source_bytes": {
            "standing": "exact",
            "basis": "work_budget_charges",
            "bytes": source_bytes,
        },
        "database_work": {
            "standing": "not_measured",
            "basis": "provider_work_units_not_exposed",
        },
        "elapsed": {
            "standing": "measured",
            "basis": "monotonic_inclusive",
            "milliseconds": elapsed_ms,
        },
        "search_totals": search_totals,
        "derived_state_counts": _state_counts(measurement),
        "derived_physical_bytes": _physical_bytes(measurement),
        "lock_or_outage": {
            "standing": "observed" if first_outage else "not_observed",
            "operation": first_outage,
        },
        "purge": {
            "standing": "unavailable",
            "basis": "destructive_evidence_not_collected",
            "counts": {
                "episodes": None,
                "reconciliation": None,
                "supersessions": None,
            },
        },
        "rebuild": {
            "standing": "unavailable",
            "basis": "purge_not_exercised",
        },
        "full_removal": {
            "standing": "unavailable",
            "basis": (
                "shared_arango_remove_all_prohibited"
                if name == "arango"
                else "owned_disposable_provider_not_declared"
            ),
            "residual_count": None,
            "declared_losses": list(_DECLARED_LOSSES[name]),
        },
    }


def run_stage2a(experiment: Stage2AExperiment) -> dict[str, object]:
    report: dict[str, object] = {
        "stage": "2A",
        "contract_version": CONTRACT_VERSION,
        "source_basis": "synthetic_only",
        "phase_a_scope": {
            "mechanics_only": True,
            "rationale_usefulness_proven": False,
            "phase_b_authorized": False,
        },
        "providers": {},
        "decision": "phase_a_checkpoint_only",
    }
    providers = report["providers"]
    assert isinstance(providers, dict)
    for name, provider in (
        ("arango", experiment.arango),
        ("sqlite", experiment.sqlite),
    ):
        providers[name] = _run_provider(name, provider, experiment)

    _validate_public_report(report)
    encoded = json.dumps(report, sort_keys=True, allow_nan=False)
    for private_value in experiment.private_values:
        if not isinstance(private_value, str) or not private_value:
            raise PublicReportError("private values must be non-empty strings")
        if private_value in encoded:
            raise PublicReportError("private value reached the public report")
    return report


def _exact_keys(value: object, expected: set[str] | frozenset[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise PublicReportError(f"{label} keys must be exactly {sorted(expected)!r}")
    return value


def _nonnegative_integer(value: object, label: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicReportError(f"{label} must be a non-negative integer")


def _validate_provider_record(name: str, value: object) -> None:
    record = _exact_keys(value, _PROVIDER_RECORD_KEYS, "provider record")
    if record["standing"] not in {"available", "unavailable"}:
        raise PublicReportError("provider standing is invalid")
    basis = _exact_keys(
        record["retrieval_basis"], set(_DESCRIPTORS[name]), "retrieval basis"
    )
    if basis != _DESCRIPTORS[name] and basis != _empty_retrieval_basis(name):
        raise PublicReportError("retrieval basis is not a frozen public descriptor")
    readiness = _exact_keys(record["schema_readiness"], {"standing"}, "readiness")
    if readiness["standing"] not in {"available", "rebuilding", "unavailable"}:
        raise PublicReportError("schema readiness standing is invalid")

    source_bytes = _exact_keys(
        record["source_bytes"], {"standing", "basis", "bytes"}, "source bytes"
    )
    if source_bytes["standing"] != "exact" or source_bytes["basis"] != (
        "work_budget_charges"
    ):
        raise PublicReportError("source byte basis is invalid")
    _nonnegative_integer(source_bytes["bytes"], "source bytes")

    if record["database_work"] != {
        "standing": "not_measured",
        "basis": "provider_work_units_not_exposed",
    }:
        raise PublicReportError("database work basis is invalid")
    elapsed = _exact_keys(
        record["elapsed"], {"standing", "basis", "milliseconds"}, "elapsed"
    )
    milliseconds = elapsed["milliseconds"]
    if (
        elapsed["standing"] != "measured"
        or elapsed["basis"] != "monotonic_inclusive"
        or isinstance(milliseconds, bool)
        or not isinstance(milliseconds, (int, float))
        or not math.isfinite(milliseconds)
        or milliseconds < 0
    ):
        raise PublicReportError("elapsed measurement is invalid")

    totals = record["search_totals"]
    if not isinstance(totals, list):
        raise PublicReportError("search totals must be a list")
    for index, total_value in enumerate(totals, start=1):
        total = _exact_keys(
            total_value,
            {"query_token", "returned_count", "total_matches", "total_standing"},
            "search total",
        )
        if total["query_token"] != f"query-{index:03d}":
            raise PublicReportError("query token is invalid")
        standing = total["total_standing"]
        if standing == "exact":
            _nonnegative_integer(total["returned_count"], "returned count")
            _nonnegative_integer(total["total_matches"], "total matches")
            if total["total_matches"] < total["returned_count"]:
                raise PublicReportError("total matches do not cover returned count")
        elif standing == "unknown":
            _nonnegative_integer(total["returned_count"], "returned count")
            if total["total_matches"] is not None:
                raise PublicReportError("unknown total includes a count")
        elif standing == "unavailable":
            if total["returned_count"] is not None or total["total_matches"] is not None:
                raise PublicReportError("unavailable total includes counts")
        else:
            raise PublicReportError("search total standing is invalid")

    state = _exact_keys(
        record["derived_state_counts"],
        {"standing", "basis", "counts"},
        "derived state counts",
    )
    if state["standing"] not in {"available", "unavailable"} or state["basis"] != (
        "provider_measurement"
    ):
        raise PublicReportError("derived state count basis is invalid")
    if not isinstance(state["counts"], dict) or not set(state["counts"]) <= _COUNT_FIELDS:
        raise PublicReportError("derived state count keys are invalid")
    for count in state["counts"].values():
        _nonnegative_integer(count, "derived state count", nullable=True)

    physical = _exact_keys(
        record["derived_physical_bytes"],
        {"standing", "basis", "total_bytes", "artifacts"},
        "derived physical bytes",
    )
    artifacts = physical["artifacts"]
    if physical["standing"] == "available":
        if physical["basis"] != "provider_reported_file_stats":
            raise PublicReportError("physical byte basis is invalid")
        if not isinstance(artifacts, dict) or set(artifacts) != {
            "database",
            "wal",
            "shm",
        }:
            raise PublicReportError("physical byte artifacts are invalid")
        total_bytes = 0
        for artifact_value in artifacts.values():
            artifact = _exact_keys(
                artifact_value, {"standing", "bytes"}, "physical byte artifact"
            )
            if artifact["standing"] not in {"available", "absent"}:
                raise PublicReportError("physical artifact standing is invalid")
            _nonnegative_integer(artifact["bytes"], "physical artifact bytes")
            total_bytes += artifact["bytes"]
        if physical["total_bytes"] != total_bytes:
            raise PublicReportError("physical byte total is invalid")
    elif physical != {
        "standing": "unavailable",
        "basis": "provider_did_not_report_physical_bytes",
        "total_bytes": None,
        "artifacts": {},
    }:
        raise PublicReportError("unavailable physical byte evidence is invalid")

    outage = _exact_keys(
        record["lock_or_outage"], {"standing", "operation"}, "lock or outage"
    )
    if outage["standing"] == "not_observed":
        if outage["operation"] is not None:
            raise PublicReportError("unobserved outage names an operation")
    elif outage["standing"] == "observed":
        if outage["operation"] not in {
            "capabilities",
            "ensure",
            "reconcile",
            "search",
            "measure",
        }:
            raise PublicReportError("observed outage operation is invalid")
    else:
        raise PublicReportError("lock or outage standing is invalid")

    if record["purge"] != {
        "standing": "unavailable",
        "basis": "destructive_evidence_not_collected",
        "counts": {
            "episodes": None,
            "reconciliation": None,
            "supersessions": None,
        },
    }:
        raise PublicReportError("purge evidence is invalid")
    if record["rebuild"] != {
        "standing": "unavailable",
        "basis": "purge_not_exercised",
    }:
        raise PublicReportError("rebuild evidence is invalid")
    expected_removal_basis = (
        "shared_arango_remove_all_prohibited"
        if name == "arango"
        else "owned_disposable_provider_not_declared"
    )
    if record["full_removal"] != {
        "standing": "unavailable",
        "basis": expected_removal_basis,
        "residual_count": None,
        "declared_losses": _DECLARED_LOSSES[name],
    }:
        raise PublicReportError("full removal evidence is invalid")


def _validate_public_report(report: object) -> None:
    public = _exact_keys(
        report,
        {
            "stage",
            "contract_version",
            "source_basis",
            "phase_a_scope",
            "providers",
            "decision",
        },
        "public report",
    )
    if public["stage"] != "2A" or public["contract_version"] != CONTRACT_VERSION:
        raise PublicReportError("stage or contract version is invalid")
    if public["source_basis"] != "synthetic_only":
        raise PublicReportError("source basis must be synthetic_only")
    if public["decision"] != "phase_a_checkpoint_only":
        raise PublicReportError("decision must be phase_a_checkpoint_only")
    if public["phase_a_scope"] != {
        "mechanics_only": True,
        "rationale_usefulness_proven": False,
        "phase_b_authorized": False,
    }:
        raise PublicReportError("Phase A scope declaration is invalid")
    providers = _exact_keys(public["providers"], set(_PROVIDER_NAMES), "providers")
    for name in _PROVIDER_NAMES:
        _validate_provider_record(name, providers[name])


def write_report_atomic(path: Path, report: dict[str, object]) -> None:
    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    _validate_public_report(report)
    payload = json.dumps(
        report, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
