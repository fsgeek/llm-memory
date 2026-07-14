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
_SCHEMA_OBJECTS = (
    ("table", "provider_meta", _SCHEMA_STATEMENTS[0]),
    ("table", "source_states", _SCHEMA_STATEMENTS[1]),
    ("table", "episode_documents", _SCHEMA_STATEMENTS[2]),
    ("table", "episode_fts", _SCHEMA_STATEMENTS[3]),
    ("table", "supersessions", _SCHEMA_STATEMENTS[4]),
    ("trigger", "episode_ai", _SCHEMA_STATEMENTS[5]),
    ("trigger", "episode_ad", _SCHEMA_STATEMENTS[6]),
    ("trigger", "episode_au", _SCHEMA_STATEMENTS[7]),
)
_FTS_SHADOW_OBJECTS = {
    "episode_fts_config": "table",
    "episode_fts_content": "table",
    "episode_fts_data": "table",
    "episode_fts_docsize": "table",
    "episode_fts_idx": "table",
}


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


def _is_missing_fts(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(
        evidence in message
        for evidence in (
            "no such module: fts5",
            "no such tokenizer",
            "unknown tokenizer",
            "error in tokenizer constructor",
            "parse error in tokenize directive",
        )
    )


def _unavailable(exc: sqlite3.OperationalError) -> ProviderUnavailable:
    return ProviderUnavailable(f"SQLite operation unavailable: {exc}")


def _normalized_sql(sql: str) -> str:
    return "".join(sql.lower().split()).replace("ifnotexists", "")


def _is_document_conflict(exc: sqlite3.IntegrityError) -> bool:
    return (
        exc.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE
        and str(exc)
        in {
            "UNIQUE constraint failed: episode_documents.storage_key",
            "UNIQUE constraint failed: episode_documents.generation_id, "
            "episode_documents.episode_ref",
        }
    )


class SQLiteStore:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 250):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if journal_mode.lower() != "wal":
                connection.close()
                raise ProviderUnsupported(
                    f"SQLite WAL journal mode is unavailable: {journal_mode}"
                )
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
            if _is_missing_fts(exc):
                raise ProviderUnsupported(
                    "SQLite FTS5 porter/unicode61 is unavailable"
                ) from exc
            raise

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
                self._validate_schema_object(
                    connection, "table", "provider_meta", _SCHEMA_STATEMENTS[0]
                )
                version = connection.execute(
                    "SELECT value FROM provider_meta WHERE key = 'schema_version'"
                ).fetchone()
                if version is None:
                    raise ProviderUnsupported("SQLite schema version is missing")
                if version[0] != str(SCHEMA_VERSION):
                    raise ProviderUnsupported(
                        f"unsupported SQLite schema version {version[0]}"
                    )
                self._validate_schema(connection)
            else:
                existing = connection.execute(
                    "SELECT name FROM sqlite_schema "
                    "WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                if existing is not None:
                    raise ProviderUnsupported(
                        f"cannot adopt preexisting schema object {existing[0]!r}"
                    )
                for statement in _SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO provider_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                self._validate_schema(connection)
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

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        for object_type, name, sql in _SCHEMA_OBJECTS:
            self._validate_schema_object(connection, object_type, name, sql)
        expected = {name: object_type for object_type, name, _ in _SCHEMA_OBJECTS}
        expected.update(_FTS_SHADOW_OBJECTS)
        observed: dict[str, str] = {}
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_schema ORDER BY name"
        ):
            object_type, name, sql = row
            if (
                object_type == "index"
                and name.startswith("sqlite_autoindex_")
                and sql is None
            ):
                continue
            observed[name] = object_type
            if expected.get(name) != object_type:
                raise ProviderUnsupported(
                    f"unexpected SQLite schema object {name!r}"
                )
        missing = expected.keys() - observed.keys()
        if missing:
            name = min(missing)
            raise ProviderUnsupported(f"missing SQLite schema object {name!r}")

    def _validate_schema_object(
        self,
        connection: sqlite3.Connection,
        object_type: str,
        name: str,
        expected_sql: str,
    ) -> None:
        actual = connection.execute(
            "SELECT type, sql FROM sqlite_schema WHERE name = ?", (name,)
        ).fetchone()
        if (
            actual is None
            or actual[0] != object_type
            or _normalized_sql(actual[1]) != _normalized_sql(expected_sql)
        ):
            raise ProviderUnsupported(f"incompatible SQLite schema object {name!r}")

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
            if _is_document_conflict(exc):
                raise SQLiteDocumentConflict(
                    f"conflicting episode document {document['storage_key']!r}"
                ) from exc
            raise
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
