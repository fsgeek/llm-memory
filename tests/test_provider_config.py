import pytest

from llm_memory.arango_provider import ArangoProvider
from llm_memory.provider_config import load_provider
from llm_memory.sqlite_provider import SQLiteProvider


def test_default_provider_remains_arango(monkeypatch):
    database = object()
    monkeypatch.delenv("LLM_MEMORY_PROVIDER", raising=False)
    monkeypatch.setattr("llm_memory.provider_config.get_database", lambda: database)

    provider = load_provider()

    assert isinstance(provider, ArangoProvider)
    assert provider._db is database


def test_explicit_arango_provider_uses_arango_database(monkeypatch):
    database = object()
    monkeypatch.setenv("LLM_MEMORY_PROVIDER", "arango")
    monkeypatch.setattr("llm_memory.provider_config.get_database", lambda: database)

    provider = load_provider()

    assert isinstance(provider, ArangoProvider)
    assert provider._db is database


def test_sqlite_selection_never_connects_to_arango(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "episodes.sqlite3"
    monkeypatch.setenv("LLM_MEMORY_PROVIDER", "sqlite")
    monkeypatch.setenv("LLM_MEMORY_SQLITE_PATH", str(sqlite_path))
    monkeypatch.setattr(
        "llm_memory.provider_config.get_database",
        lambda: (_ for _ in ()).throw(AssertionError("Arango must stay lazy")),
    )

    provider = load_provider()

    assert isinstance(provider, SQLiteProvider)
    assert provider.store.path == sqlite_path


@pytest.mark.parametrize("provider_name", ["", "fallback", "SQLite", " sqlite"])
def test_unknown_or_malformed_provider_fails_visibly(provider_name, monkeypatch):
    monkeypatch.setenv("LLM_MEMORY_PROVIDER", provider_name)

    with pytest.raises(ValueError, match="LLM_MEMORY_PROVIDER"):
        load_provider()


def test_sqlite_requires_configured_path(monkeypatch):
    monkeypatch.setenv("LLM_MEMORY_PROVIDER", "sqlite")
    monkeypatch.delenv("LLM_MEMORY_SQLITE_PATH", raising=False)

    with pytest.raises(ValueError, match="LLM_MEMORY_SQLITE_PATH"):
        load_provider()


def test_sqlite_rejects_empty_path(monkeypatch):
    monkeypatch.setenv("LLM_MEMORY_PROVIDER", "sqlite")
    monkeypatch.setenv("LLM_MEMORY_SQLITE_PATH", "")

    with pytest.raises(ValueError, match="LLM_MEMORY_SQLITE_PATH"):
        load_provider()
