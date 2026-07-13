from pathlib import Path

import pytest
import yaml

from llm_memory.enrollment import SourceEnrollment, load_registry


VALID_CONFIG = {
    "contract_version": 1,
    "sources": [
        {
            "corpus_id": "project-history",
            "source_id": "claude-sessions",
            "adapter": "claude_code_jsonl",
            "boundary_version": 1,
            "canonicalization_version": 1,
            "locator": "/tmp/claude",
            "enabled": True,
            "full_validation_max_age_seconds": 86400,
        },
        {
            "corpus_id": "project-history",
            "source_id": "gateway-log",
            "adapter": "gateway_jsonl",
            "boundary_version": 1,
            "canonicalization_version": 1,
            "locator": "/tmp/gateway.jsonl",
            "enabled": True,
            "full_validation_max_age_seconds": 86400,
        },
    ],
}


def write_config(tmp_path: Path, config: object = VALID_CONFIG) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_registry_returns_only_locally_enrolled_sources(tmp_path, monkeypatch):
    def database_access_is_forbidden(*args, **kwargs):
        raise AssertionError("enrollment must not inspect database contents")

    monkeypatch.setattr(
        "llm_memory.db.get_database", database_access_is_forbidden
    )

    registry = load_registry(write_config(tmp_path))

    assert registry.known_corpora == frozenset({"project-history"})
    assert registry.sources_for("project-history") == (
        SourceEnrollment(
            corpus_id="project-history",
            source_id="claude-sessions",
            adapter="claude_code_jsonl",
            boundary_version=1,
            canonicalization_version=1,
            locator=Path("/tmp/claude"),
            enabled=True,
            full_validation_max_age_seconds=86400,
        ),
        SourceEnrollment(
            corpus_id="project-history",
            source_id="gateway-log",
            adapter="gateway_jsonl",
            boundary_version=1,
            canonicalization_version=1,
            locator=Path("/tmp/gateway.jsonl"),
            enabled=True,
            full_validation_max_age_seconds=86400,
        ),
    )


def test_sources_for_excludes_disabled_sources_by_default(tmp_path):
    config = VALID_CONFIG | {
        "sources": [VALID_CONFIG["sources"][0] | {"enabled": False}]
    }
    registry = load_registry(write_config(tmp_path, config))

    assert registry.sources_for("project-history") == ()
    assert len(registry.sources_for("project-history", enabled_only=False)) == 1
    assert registry.known_corpora == frozenset({"project-history"})


def test_registry_rejects_duplicate_corpus_and_source_pair(tmp_path):
    source = VALID_CONFIG["sources"][0]
    path = write_config(tmp_path, VALID_CONFIG | {"sources": [source, source]})

    with pytest.raises(ValueError, match="duplicate.*corpus_id.*source_id"):
        load_registry(path)


def test_registry_rejects_unsupported_top_level_keys(tmp_path):
    path = write_config(tmp_path, VALID_CONFIG | {"database": "not-authoritative"})

    with pytest.raises(ValueError, match="top-level keys"):
        load_registry(path)


def test_registry_rejects_missing_locator(tmp_path):
    source = VALID_CONFIG["sources"][0].copy()
    del source["locator"]
    path = write_config(tmp_path, VALID_CONFIG | {"sources": [source]})

    with pytest.raises(ValueError, match="source keys"):
        load_registry(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("boundary_version", 0),
        ("canonicalization_version", -1),
        ("full_validation_max_age_seconds", 0),
    ],
)
def test_registry_rejects_non_positive_semantic_integers(tmp_path, field, value):
    source = VALID_CONFIG["sources"][0] | {field: value}
    path = write_config(tmp_path, VALID_CONFIG | {"sources": [source]})

    with pytest.raises(ValueError, match=f"{field} must be a positive integer"):
        load_registry(path)


@pytest.mark.parametrize(
    ("adapter", "boundary_version", "canonicalization_version"),
    [
        ("taste_open_jsonl", 2, 1),
        ("gateway_jsonl", 1, 2),
        ("claude_code_jsonl", 2, 2),
    ],
)
def test_registry_rejects_unimplemented_adapter_semantic_versions(
    tmp_path, adapter, boundary_version, canonicalization_version
):
    source = VALID_CONFIG["sources"][0] | {
        "adapter": adapter,
        "boundary_version": boundary_version,
        "canonicalization_version": canonicalization_version,
    }
    path = write_config(tmp_path, VALID_CONFIG | {"sources": [source]})

    with pytest.raises(ValueError, match="semantic versions"):
        load_registry(path)


def test_environment_overrides_default_sources_path(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    monkeypatch.setenv("LLM_MEMORY_SOURCES_CONFIG", str(path))

    registry = load_registry()

    assert registry.known_corpora == frozenset({"project-history"})


@pytest.mark.parametrize(
    ("field", "identifier"),
    [
        ("corpus_id", ""),
        ("corpus_id", "contains/slash"),
        ("corpus_id", "contains space"),
        ("corpus_id", "caf\u00e9"),
        ("corpus_id", "wild*"),
        ("source_id", ""),
        ("source_id", "contains/slash"),
    ],
)
def test_registry_rejects_invalid_identifiers(tmp_path, field, identifier):
    source = VALID_CONFIG["sources"][0] | {field: identifier}
    path = write_config(tmp_path, VALID_CONFIG | {"sources": [source]})

    with pytest.raises(ValueError, match=field):
        load_registry(path)


@pytest.mark.parametrize("adapter", ["unknown", []])
def test_registry_rejects_unsupported_adapter(tmp_path, adapter):
    source = VALID_CONFIG["sources"][0] | {"adapter": adapter}
    path = write_config(tmp_path, VALID_CONFIG | {"sources": [source]})

    with pytest.raises(ValueError, match="unsupported adapter"):
        load_registry(path)


@pytest.mark.parametrize("version", [2, 1.0, True])
def test_registry_rejects_unsupported_contract_version(tmp_path, version):
    path = write_config(tmp_path, VALID_CONFIG | {"contract_version": version})

    with pytest.raises(ValueError, match="contract_version must be 1"):
        load_registry(path)


def test_registry_retains_relative_locator_without_resolving_it(tmp_path):
    source = VALID_CONFIG["sources"][0] | {"locator": "relative/source.jsonl"}
    path = write_config(tmp_path, VALID_CONFIG | {"sources": [source]})

    enrollment = load_registry(path).sources[0]

    assert enrollment.locator == Path("relative/source.jsonl")
    assert not enrollment.locator.is_absolute()
