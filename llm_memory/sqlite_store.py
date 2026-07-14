from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from llm_memory.provider import ProviderUnavailable, ProviderUnsupported


SCHEMA_VERSION = 1
_EPISODE_COLUMNS = (
    "storage_key",
    "corpus_id",
    "source_id",
    "member_id",
    "generation_id",
    "episode_ref",
    "reference_key",
    "timestamp",
    "user_message",
    "response",
    "state_text",
    "document_json",
)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS provider_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_states (
      state_key TEXT PRIMARY KEY,
      corpus_id TEXT NOT NULL,
      source_id TEXT NOT NULL,
      member_id TEXT NOT NULL,
      revision INTEGER NOT NULL,
      state_json TEXT NOT NULL,
      UNIQUE(corpus_id, source_id, member_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS episode_documents (
      rowid INTEGER PRIMARY KEY,
      storage_key TEXT NOT NULL UNIQUE,
      corpus_id TEXT NOT NULL,
      source_id TEXT NOT NULL,
      member_id TEXT NOT NULL,
      generation_id TEXT NOT NULL,
      episode_ref TEXT NOT NULL,
      reference_key TEXT NOT NULL,
      timestamp TEXT NOT NULL,
      user_message TEXT NOT NULL,
      response TEXT NOT NULL,
      state_text TEXT NOT NULL,
      document_json TEXT NOT NULL,
      UNIQUE(generation_id, episode_ref)
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS episode_fts USING fts5(
      user_message, response, state_text,
      storage_key UNINDEXED, corpus_id UNINDEXED, generation_id UNINDEXED,
      tokenize='porter unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS supersessions (
      observation_key TEXT PRIMARY KEY,
      corpus_id TEXT NOT NULL,
      source_id TEXT NOT NULL,
      member_id TEXT NOT NULL,
      event_token TEXT NOT NULL,
      old_ref TEXT NOT NULL,
      new_ref TEXT NOT NULL,
      reason TEXT NOT NULL,
      detected_at TEXT NOT NULL,
      UNIQUE(old_ref, new_ref)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS episode_ai AFTER INSERT ON episode_documents BEGIN
      INSERT INTO episode_fts(
        rowid, user_message, response, state_text,
        storage_key, corpus_id, generation_id
      ) VALUES(
        new.rowid, new.user_message, new.response, new.state_text,
        new.storage_key, new.corpus_id, new.generation_id
      );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS episode_ad AFTER DELETE ON episode_documents BEGIN
      DELETE FROM episode_fts WHERE rowid=old.rowid;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS episode_au AFTER UPDATE ON episode_documents BEGIN
      DELETE FROM episode_fts WHERE rowid=old.rowid;
      INSERT INTO episode_fts(
        rowid, user_message, response, state_text,
        storage_key, corpus_id, generation_id
      ) VALUES(
        new.rowid, new.user_message, new.response, new.state_text,
        new.storage_key, new.corpus_id, new.generation_id
      );
    END
    """,
)


@dataclass(frozen=True)
class SQLiteSchemaStanding:
    provider: str = "sqlite"
    schema_version: int = SCHEMA_VERSION
    index_standing: str = "available"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class SQLiteStateConflict(RuntimeError):
    pass


class SQLiteDocumentConflict(RuntimeError):
    pass


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _unavailable(exc: sqlite3.OperationalError) -> ProviderUnavailable:
    return ProviderUnavailable(f"SQLite operation unavailable: {exc}")


class SQLiteStore:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 250):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            return connection
        except sqlite3.OperationalError as exc:
            if "connection" in locals():
                connection.close()
            if _is_busy(exc):
                raise _unavailable(exc) from exc
            raise

    def _probe_fts5(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5("
                "value, tokenize='porter unicode61 remove_diacritics 2')"
            )
            connection.execute("DROP TABLE temp.__fts5_probe")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise _unavailable(exc) from exc
            raise ProviderUnsupported(
                "SQLite FTS5 porter/unicode61 is unavailable"
            ) from exc

    def ensure(self) -> SQLiteSchemaStanding:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            self._probe_fts5(connection)
            connection.execute("BEGIN IMMEDIATE")
            has_meta = connection.execute(
                "SELECT 1 FROM sqlite_schema "
                "WHERE type = 'table' AND name = 'provider_meta'"
            ).fetchone()
            if has_meta:
                version = connection.execute(
                    "SELECT value FROM provider_meta WHERE key = 'schema_version'"
                ).fetchone()
                if version is None:
                    raise ProviderUnsupported("SQLite schema version is missing")
                if version[0] != str(SCHEMA_VERSION):
                    raise ProviderUnsupported(
                        f"unsupported SQLite schema version {version[0]}"
                    )

            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO provider_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            connection.commit()
            return SQLiteSchemaStanding()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if _is_busy(exc):
                raise _unavailable(exc) from exc
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._transaction("BEGIN") as connection:
            yield connection

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._transaction("BEGIN IMMEDIATE") as connection:
            yield connection

    @contextmanager
    def _transaction(self, begin: str) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute(begin)
            yield connection
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if _is_busy(exc):
                raise _unavailable(exc) from exc
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def insert_episode(
        self, connection: sqlite3.Connection, document: Mapping[str, Any]
    ) -> int:
        values = tuple(document[column] for column in _EPISODE_COLUMNS)
        placeholders = ", ".join("?" for _ in _EPISODE_COLUMNS)
        columns = ", ".join(_EPISODE_COLUMNS)
        try:
            cursor = connection.execute(
                f"INSERT INTO episode_documents({columns}) VALUES ({placeholders})",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise SQLiteDocumentConflict(
                f"conflicting episode document {document['storage_key']!r}"
            ) from exc
        return cursor.lastrowid

    def delete_episode(self, connection: sqlite3.Connection, rowid: int) -> None:
        connection.execute("DELETE FROM episode_documents WHERE rowid = ?", (rowid,))

    def fts_row(self, rowid: int) -> sqlite3.Row | None:
        try:
            with self.read_transaction() as connection:
                return connection.execute(
                    "SELECT rowid, * FROM episode_fts WHERE rowid = ?", (rowid,)
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise _unavailable(exc) from exc
            raise
