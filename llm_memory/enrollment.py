from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from llm_memory.adapter_versions import supports_semantic_versions
from llm_memory.contract import CONTRACT_VERSION, validate_corpus_id


DEFAULT_SOURCES_PATH = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"

_TOP_LEVEL_KEYS = frozenset({"contract_version", "sources"})
_SOURCE_KEYS = frozenset(
    {
        "corpus_id",
        "source_id",
        "adapter",
        "boundary_version",
        "canonicalization_version",
        "locator",
        "enabled",
        "full_validation_max_age_seconds",
    }
)
_SUPPORTED_ADAPTERS = frozenset(
    {"taste_open_jsonl", "gateway_jsonl", "claude_code_jsonl"}
)


def _require_identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or not value or "/" in value:
        raise ValueError(f"{name} must be non-empty and contain no '/'")


def _require_positive_integer(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class SourceEnrollment:
    corpus_id: str
    source_id: str
    adapter: str
    boundary_version: int
    canonicalization_version: int
    locator: Path
    enabled: bool
    full_validation_max_age_seconds: int

    def __post_init__(self) -> None:
        validate_corpus_id(self.corpus_id)
        _require_identifier("source_id", self.source_id)
        if not isinstance(self.adapter, str) or self.adapter not in _SUPPORTED_ADAPTERS:
            raise ValueError(f"unsupported adapter: {self.adapter!r}")
        _require_positive_integer("boundary_version", self.boundary_version)
        _require_positive_integer(
            "canonicalization_version", self.canonicalization_version
        )
        if not isinstance(self.locator, Path):
            raise ValueError("locator must be a path")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        _require_positive_integer(
            "full_validation_max_age_seconds",
            self.full_validation_max_age_seconds,
        )


@dataclass(frozen=True)
class EnrollmentRegistry:
    sources: tuple[SourceEnrollment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.sources, tuple) or not all(
            isinstance(source, SourceEnrollment) for source in self.sources
        ):
            raise ValueError("sources must be a tuple of SourceEnrollment declarations")

        identities: set[tuple[str, str]] = set()
        for source in self.sources:
            identity = (source.corpus_id, source.source_id)
            if identity in identities:
                raise ValueError(
                    "duplicate (corpus_id, source_id) enrollment: "
                    f"{source.corpus_id!r}, {source.source_id!r}"
                )
            identities.add(identity)

    @property
    def known_corpora(self) -> frozenset[str]:
        return frozenset(source.corpus_id for source in self.sources)

    def sources_for(
        self, corpus_id: str, *, enabled_only: bool = True
    ) -> tuple[SourceEnrollment, ...]:
        return tuple(
            source
            for source in self.sources
            if source.corpus_id == corpus_id and (source.enabled or not enabled_only)
        )


def _require_exact_keys(
    value: object, expected: frozenset[str], description: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{description} must be exactly {sorted(expected)!r}")
    return value


def _parse_source(value: object) -> SourceEnrollment:
    source = _require_exact_keys(value, _SOURCE_KEYS, "source keys")
    locator = source["locator"]
    if not isinstance(locator, str) or not locator:
        raise ValueError("locator must be a non-empty string")
    enrollment = SourceEnrollment(
        corpus_id=source["corpus_id"],
        source_id=source["source_id"],
        adapter=source["adapter"],
        boundary_version=source["boundary_version"],
        canonicalization_version=source["canonicalization_version"],
        locator=Path(locator),
        enabled=source["enabled"],
        full_validation_max_age_seconds=source[
            "full_validation_max_age_seconds"
        ],
    )
    if not supports_semantic_versions(
        enrollment.adapter,
        boundary_version=enrollment.boundary_version,
        canonicalization_version=enrollment.canonicalization_version,
    ):
        raise ValueError(
            "unsupported adapter semantic versions: "
            f"{enrollment.adapter} boundary={enrollment.boundary_version} "
            f"canonicalization={enrollment.canonicalization_version}"
        )
    return enrollment


def load_registry(path: Path | None = None) -> EnrollmentRegistry:
    if path is None:
        configured_path = os.environ.get("LLM_MEMORY_SOURCES_CONFIG")
        path = Path(configured_path) if configured_path else DEFAULT_SOURCES_PATH
    else:
        path = Path(path)

    with path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)

    config = _require_exact_keys(config, _TOP_LEVEL_KEYS, "top-level keys")
    version = config["contract_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != CONTRACT_VERSION
    ):
        raise ValueError(f"contract_version must be {CONTRACT_VERSION}")
    sources = config["sources"]
    if not isinstance(sources, list):
        raise ValueError("sources must be a list")

    return EnrollmentRegistry(tuple(_parse_source(source) for source in sources))
