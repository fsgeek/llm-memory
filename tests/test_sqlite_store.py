from __future__ import annotations

import sqlite3

import pytest

from llm_memory.provider import ProviderUnavailable, ProviderUnsupported
from llm_memory.sqlite_store import SQLiteDocumentConflict, SQLiteStore


@pytest.fixture
def sqlite_store(tmp_path):
    store = SQLiteStore(tmp_path / "episodes.sqlite3", busy_timeout_ms=20)
    store.ensure()
    return store


@pytest.fixture
def episode_row():
    return {
        "storage_key": "storage-a",
        "corpus_id": "corpus-a",
        "source_id": "source-a",
        "member_id": "member-a",
        "generation_id": "generation-a",
        "episode_ref": "episode-a",
        "reference_key": "reference-a",
        "timestamp": "2026-07-14T10:00:00Z",
        "user_message": "How is cafe indexed?",
        "response": "With a porter tokenizer.",
        "state_text": "status observed",
        "document_json": '{"episode_ref":"episode-a"}',
    }


def test_ensure_creates_schema_version_one_and_is_idempotent(tmp_path):
    store = SQLiteStore(tmp_path / "episodes.sqlite3", busy_timeout_ms=50)

    assert store.ensure().as_dict() == {
        "provider": "sqlite",
        "schema_version": 1,
        "index_standing": "available",
    }
    assert store.ensure().schema_version == 1
    with store.connect() as connection:
        assert connection.execute(
            "SELECT value FROM provider_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "1"


def test_ensure_creates_exact_fts5_configuration(tmp_path):
    store = SQLiteStore(tmp_path / "episodes.sqlite3", busy_timeout_ms=50)
    assert store.ensure().index_standing == "available"
    with store.connect() as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'episode_fts'"
        ).fetchone()[0]
    assert "porter unicode61 remove_diacritics 2" in sql


def test_ensure_rejects_unknown_schema_version(tmp_path):
    store = SQLiteStore(tmp_path / "episodes.sqlite3")
    store.ensure()
    with store.connect() as connection:
        connection.execute(
            "UPDATE provider_meta SET value = '2' WHERE key = 'schema_version'"
        )

    with pytest.raises(ProviderUnsupported, match="schema version 2"):
        store.ensure()


def test_ensure_rejects_existing_metadata_without_schema_version(tmp_path):
    path = tmp_path / "episodes.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE provider_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

    with pytest.raises(ProviderUnsupported, match="schema version is missing"):
        SQLiteStore(path).ensure()


def test_ensure_reports_unsupported_exact_fts_configuration(tmp_path, monkeypatch):
    store = SQLiteStore(tmp_path / "episodes.sqlite3")
    real_connect = sqlite3.connect

    class NoFtsConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, parameters=()):
            if sql.startswith("CREATE VIRTUAL TABLE temp.__fts5_probe"):
                raise sqlite3.OperationalError("no such module: fts5")
            return self._connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *args, **kwargs: NoFtsConnection(real_connect(*args, **kwargs)),
    )
    with pytest.raises(ProviderUnsupported, match="porter/unicode61"):
        store.ensure()


def test_ensure_translates_probe_lock_to_unavailable(tmp_path):
    store = SQLiteStore(tmp_path / "episodes.sqlite3")

    class LockedConnection:
        def execute(self, _sql):
            raise sqlite3.OperationalError("database is locked")

    with pytest.raises(ProviderUnavailable, match="locked"):
        store._probe_fts5(LockedConnection())


def test_connections_are_independent_and_apply_required_pragmas(sqlite_store):
    first = sqlite_store.connect()
    second = sqlite_store.connect()
    try:
        assert first is not second
        assert first.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert second.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert first.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert second.execute("PRAGMA busy_timeout").fetchone()[0] == 20

        first.execute("BEGIN")
        first.execute(
            "INSERT INTO provider_meta(key, value) VALUES ('isolation', 'pending')"
        )
        assert second.execute(
            "SELECT value FROM provider_meta WHERE key = 'isolation'"
        ).fetchone() is None
        first.rollback()
    finally:
        first.close()
        second.close()


def test_episode_triggers_keep_fts_copy_transactional(sqlite_store, episode_row):
    with sqlite_store.write_transaction() as connection:
        rowid = sqlite_store.insert_episode(connection, episode_row)
    assert sqlite_store.fts_row(rowid)["user_message"] == episode_row["user_message"]
    with pytest.raises(RuntimeError, match="crash"):
        with sqlite_store.write_transaction() as connection:
            sqlite_store.delete_episode(connection, rowid)
            raise RuntimeError("crash")
    assert sqlite_store.fts_row(rowid) is not None


def test_episode_update_and_delete_triggers_keep_fts_in_sync(sqlite_store, episode_row):
    with sqlite_store.write_transaction() as connection:
        rowid = sqlite_store.insert_episode(connection, episode_row)
        connection.execute(
            "UPDATE episode_documents SET response = ? WHERE rowid = ?",
            ("Updated response", rowid),
        )
    assert sqlite_store.fts_row(rowid)["response"] == "Updated response"

    with sqlite_store.write_transaction() as connection:
        sqlite_store.delete_episode(connection, rowid)
    assert sqlite_store.fts_row(rowid) is None


def test_write_transaction_translates_bounded_lock_timeout(sqlite_store):
    blocker = sqlite_store.connect()
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(ProviderUnavailable, match="locked|busy"):
            with sqlite_store.write_transaction():
                pass
    finally:
        blocker.rollback()
        blocker.close()


def test_document_uniqueness_is_reported_as_document_conflict(
    sqlite_store, episode_row
):
    with sqlite_store.write_transaction() as connection:
        sqlite_store.insert_episode(connection, episode_row)

    with pytest.raises(SQLiteDocumentConflict) as caught:
        with sqlite_store.write_transaction() as connection:
            sqlite_store.insert_episode(connection, episode_row)
    assert not isinstance(caught.value, sqlite3.IntegrityError)
