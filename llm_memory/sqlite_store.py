from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from llm_memory.adapters import EpisodeRecord, SourceMember
from llm_memory.contract import reference_key
from llm_memory.enrollment import SourceEnrollment
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


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _state_key(corpus_id: str, source_id: str, member_id: str) -> str:
    identity = f"{corpus_id}/{source_id}/{member_id}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _generation_storage_key(generation_id: str, episode_ref: str) -> str:
    digest = hashlib.sha256()
    digest.update(generation_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(episode_ref.encode("utf-8"))
    return digest.hexdigest()


def _episode_document(
    enrollment: SourceEnrollment,
    member: SourceMember,
    generation_id: str,
    episode: EpisodeRecord,
) -> dict[str, Any]:
    episode_ref = episode.identity.episode_ref
    return {
        "storage_key": _generation_storage_key(generation_id, episode_ref),
        "episode_ref": episode_ref,
        "reference_key": reference_key(episode_ref),
        "corpus_id": enrollment.corpus_id,
        "source_id": enrollment.source_id,
        "member_id": member.member_id,
        "generation_id": generation_id,
        "canonicalization_version": enrollment.canonicalization_version,
        "boundary_version": enrollment.boundary_version,
        "body_digest": episode.identity.body_digest,
        "native_event_id": episode.native_event_id,
        "source_position": episode.source_position,
        **episode.body.as_dict(),
        "state_text": episode.state_text,
    }


def _require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("mutation requires an active explicit transaction")


def _same_episode_row(
    existing: sqlite3.Row | None,
    document: Mapping[str, Any],
    serialized: str,
) -> bool:
    if existing is None:
        return False
    return all(
        existing[column]
        == (serialized if column == "document_json" else document[column])
        for column in _EPISODE_COLUMNS
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
            "SELECT type, name FROM sqlite_schema ORDER BY name"
        ):
            object_type, name = row
            if name.startswith("sqlite_"):
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

    def source_states(self, enrollment: SourceEnrollment) -> tuple[dict[str, Any], ...]:
        with self.read_transaction() as connection:
            rows = connection.execute(
                "SELECT revision, state_json FROM source_states "
                "WHERE corpus_id = ? AND source_id = ? ORDER BY member_id",
                (enrollment.corpus_id, enrollment.source_id),
            ).fetchall()
        return tuple(self._deserialize_state(row) for row in rows)

    def member_state(
        self, enrollment: SourceEnrollment, member_id: str
    ) -> dict[str, Any] | None:
        state_key = _state_key(enrollment.corpus_id, enrollment.source_id, member_id)
        with self.read_transaction() as connection:
            row = self._state_row(connection, state_key)
        return None if row is None else self._deserialize_state(row)

    def compare_and_swap_state(
        self,
        enrollment: SourceEnrollment,
        member_id: str,
        expected: Mapping[str, Any] | None,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self.write_transaction() as connection:
            return self._cas_state_in_transaction(
                connection, enrollment, member_id, expected, values
            )

    def _state_row(
        self, connection: sqlite3.Connection, state_key: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT revision, state_json FROM source_states WHERE state_key = ?",
            (state_key,),
        ).fetchone()

    @staticmethod
    def _deserialize_state(row: sqlite3.Row) -> dict[str, Any]:
        state = json.loads(row["state_json"])
        state["revision"] = row["revision"]
        return state

    def _cas_state_in_transaction(
        self,
        connection: sqlite3.Connection,
        enrollment: SourceEnrollment,
        member_id: str,
        expected: Mapping[str, Any] | None,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        _require_transaction(connection)
        state_key = _state_key(enrollment.corpus_id, enrollment.source_id, member_id)
        row = self._state_row(connection, state_key)
        current = None if row is None else self._deserialize_state(row)
        expected_revision = None if expected is None else expected["revision"]
        current_revision = None if current is None else current["revision"]
        if current_revision != expected_revision:
            raise SQLiteStateConflict(f"source state changed for {state_key}")

        state = {} if current is None else dict(current)
        state.pop("revision", None)
        state.update(values)
        state.pop("revision", None)
        state.update(
            {
                "corpus_id": enrollment.corpus_id,
                "source_id": enrollment.source_id,
                "member_id": member_id,
            }
        )
        revision = 1 if current_revision is None else current_revision + 1
        serialized = _canonical_json(state)
        if current_revision is None:
            connection.execute(
                "INSERT INTO source_states("
                "state_key, corpus_id, source_id, member_id, revision, state_json"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    state_key,
                    enrollment.corpus_id,
                    enrollment.source_id,
                    member_id,
                    revision,
                    serialized,
                ),
            )
        else:
            cursor = connection.execute(
                "UPDATE source_states SET revision = ?, state_json = ? "
                "WHERE state_key = ? AND revision = ?",
                (revision, serialized, state_key, current_revision),
            )
            if cursor.rowcount != 1:
                raise SQLiteStateConflict(f"source state changed for {state_key}")
        return state | {"revision": revision}

    def write_generation(
        self,
        connection: sqlite3.Connection,
        enrollment: SourceEnrollment,
        member: SourceMember,
        generation_id: str,
        episodes: Iterable[EpisodeRecord],
    ) -> int:
        _require_transaction(connection)
        documents = [
            _episode_document(enrollment, member, generation_id, episode)
            for episode in episodes
        ]
        for document in documents:
            self._insert_immutable_document(connection, document)
        return len(documents)

    def _insert_immutable_document(
        self, connection: sqlite3.Connection, document: Mapping[str, Any]
    ) -> None:
        serialized = _canonical_json(document)
        existing = connection.execute(
            f"SELECT {', '.join(_EPISODE_COLUMNS)} FROM episode_documents "
            "WHERE (generation_id = ? AND episode_ref = ?) OR storage_key = ?",
            (
                document["generation_id"],
                document["episode_ref"],
                document["storage_key"],
            ),
        ).fetchone()
        if existing is not None:
            if _same_episode_row(existing, document, serialized):
                return
            raise SQLiteDocumentConflict(
                f"conflicting episode document {document['storage_key']!r}"
            )

        row = dict(document)
        row["document_json"] = serialized
        try:
            self.insert_episode(connection, row)
        except SQLiteDocumentConflict:
            existing = connection.execute(
                f"SELECT {', '.join(_EPISODE_COLUMNS)} FROM episode_documents "
                "WHERE (generation_id = ? AND episode_ref = ?) OR storage_key = ?",
                (
                    document["generation_id"],
                    document["episode_ref"],
                    document["storage_key"],
                ),
            ).fetchone()
            if _same_episode_row(existing, document, serialized):
                return
            raise

    def seed_generation(
        self,
        connection: sqlite3.Connection,
        enrollment: SourceEnrollment,
        member: SourceMember,
        source_generation_id: str,
        target_generation_id: str,
    ) -> tuple[int, float]:
        _require_transaction(connection)
        state_key = _state_key(
            enrollment.corpus_id, enrollment.source_id, member.member_id
        )
        state_row = self._state_row(connection, state_key)
        state = None if state_row is None else self._deserialize_state(state_row)
        if state is None or state.get("active_generation_id") != source_generation_id:
            raise SQLiteStateConflict(
                f"generation {source_generation_id!r} is not active for {state_key}"
            )
        started = time.perf_counter()
        rows = connection.execute(
            "SELECT document_json FROM episode_documents "
            "WHERE corpus_id = ? AND source_id = ? AND member_id = ? "
            "AND generation_id = ? ORDER BY episode_ref",
            (
                enrollment.corpus_id,
                enrollment.source_id,
                member.member_id,
                source_generation_id,
            ),
        ).fetchall()
        for row in rows:
            clone = json.loads(row["document_json"])
            clone["generation_id"] = target_generation_id
            clone["storage_key"] = _generation_storage_key(
                target_generation_id, clone["episode_ref"]
            )
            self._insert_immutable_document(connection, clone)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return len(rows), elapsed_ms

    def generation_count(
        self, connection: sqlite3.Connection, generation_id: str
    ) -> int:
        return connection.execute(
            "SELECT count(*) FROM episode_documents WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()[0]

    def verify_generation(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        *,
        expected_count: int,
    ) -> bool:
        actual_count = self.generation_count(connection, generation_id)
        indexed_count = connection.execute(
            "SELECT count(*) FROM episode_fts WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()[0]
        return (actual_count, indexed_count) == (expected_count, expected_count)

    def activate_generation(
        self,
        connection: sqlite3.Connection,
        enrollment: SourceEnrollment,
        member: SourceMember,
        generation_id: str,
        *,
        expected_count: int,
        expected_state: Mapping[str, Any],
        state_values: Mapping[str, Any] | None = None,
        supersession_observations: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        _require_transaction(connection)
        state_key = _state_key(
            enrollment.corpus_id, enrollment.source_id, member.member_id
        )
        row = self._state_row(connection, state_key)
        current = None if row is None else self._deserialize_state(row)
        if (
            expected_state.get("revision") is None
            or expected_state.get("staging_generation_id") != generation_id
            or current is None
            or current["revision"] != expected_state["revision"]
            or current.get("staging_generation_id") != generation_id
        ):
            raise SQLiteStateConflict(
                f"generation {generation_id!r} lost staging ownership"
            )
        owned_count = connection.execute(
            "SELECT count(*) FROM episode_documents "
            "WHERE corpus_id = ? AND source_id = ? AND member_id = ? "
            "AND generation_id = ?",
            (
                enrollment.corpus_id,
                enrollment.source_id,
                member.member_id,
                generation_id,
            ),
        ).fetchone()[0]
        if owned_count != expected_count or not self.verify_generation(
            connection, generation_id, expected_count=expected_count
        ):
            raise SQLiteDocumentConflict(
                f"generation {generation_id!r} is incomplete or unindexed"
            )
        for observation in supersession_observations:
            connection.execute(
                "INSERT OR IGNORE INTO supersessions("
                "observation_key, corpus_id, source_id, member_id, event_token, "
                "old_ref, new_ref, reason, detected_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(
                    observation[key]
                    for key in (
                        "observation_key",
                        "corpus_id",
                        "source_id",
                        "member_id",
                        "event_token",
                        "old_ref",
                        "new_ref",
                        "reason",
                        "detected_at",
                    )
                ),
            )
        values = {
            "active_generation_id": generation_id,
            "staging_generation_id": None,
            "staging_episode_count": None,
            "staging_canonicalization_version": None,
            "staging_boundary_version": None,
            "episode_count": expected_count,
            "active_generation_integrity": "valid",
            "freshness": "current",
            "canonicalization_version": enrollment.canonicalization_version,
            "boundary_version": enrollment.boundary_version,
        }
        if state_values:
            values.update(state_values)
        values.update(
            {
                "active_generation_id": generation_id,
                "staging_generation_id": None,
                "staging_episode_count": None,
                "episode_count": expected_count,
                "active_generation_integrity": "valid",
                "canonicalization_version": enrollment.canonicalization_version,
                "boundary_version": enrollment.boundary_version,
            }
        )
        self._cas_state_in_transaction(
            connection,
            enrollment,
            member.member_id,
            expected_state,
            values,
        )

    def delete_generation(
        self,
        connection: sqlite3.Connection,
        enrollment: SourceEnrollment,
        member: SourceMember,
        generation_id: str,
    ) -> int:
        _require_transaction(connection)
        state_key = _state_key(
            enrollment.corpus_id, enrollment.source_id, member.member_id
        )
        state_row = self._state_row(connection, state_key)
        if state_row is not None:
            state = self._deserialize_state(state_row)
            if state.get("active_generation_id") == generation_id:
                return 0
        cursor = connection.execute(
            "DELETE FROM episode_documents "
            "WHERE corpus_id = ? AND source_id = ? AND member_id = ? "
            "AND generation_id = ?",
            (
                enrollment.corpus_id,
                enrollment.source_id,
                member.member_id,
                generation_id,
            ),
        )
        return cursor.rowcount

    def insert_episode(
        self, connection: sqlite3.Connection, document: Mapping[str, Any]
    ) -> int:
        _require_transaction(connection)
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
        _require_transaction(connection)
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

    def active_episode_refs(
        self, corpus_id: str, source_id: str
    ) -> tuple[str, ...]:
        with self.read_transaction() as connection:
            rows = connection.execute(
                "SELECT episode.episode_ref "
                "FROM episode_documents AS episode "
                "JOIN source_states AS state "
                "ON state.corpus_id = episode.corpus_id "
                "AND state.source_id = episode.source_id "
                "AND state.member_id = episode.member_id "
                "AND json_extract(state.state_json, '$.active_generation_id') "
                "= episode.generation_id "
                "WHERE episode.corpus_id = ? AND episode.source_id = ? "
                "ORDER BY episode.member_id, "
                "json_extract(episode.document_json, '$.source_position.start'), "
                "episode.episode_ref",
                (corpus_id, source_id),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def staging_episode_count(self, corpus_id: str, source_id: str) -> int:
        with self.read_transaction() as connection:
            return connection.execute(
                "SELECT count(*) FROM episode_documents AS episode "
                "JOIN source_states AS state "
                "ON state.corpus_id = episode.corpus_id "
                "AND state.source_id = episode.source_id "
                "AND state.member_id = episode.member_id "
                "AND json_extract(state.state_json, '$.staging_generation_id') "
                "= episode.generation_id "
                "WHERE episode.corpus_id = ? AND episode.source_id = ?",
                (corpus_id, source_id),
            ).fetchone()[0]

    def generation_documents(
        self,
        enrollment: SourceEnrollment,
        member_id: str,
        generation_id: str,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        clauses = [
            "corpus_id = ?",
            "source_id = ?",
            "member_id = ?",
            "generation_id = ?",
        ]
        parameters: list[Any] = [
            enrollment.corpus_id,
            enrollment.source_id,
            member_id,
            generation_id,
        ]
        if start is not None:
            clauses.append(
                "json_extract(document_json, '$.source_position.start') >= ?"
            )
            parameters.append(start)
        if end is not None:
            clauses.append(
                "json_extract(document_json, '$.source_position.end') <= ?"
            )
            parameters.append(end)
        with self.read_transaction() as connection:
            rows = connection.execute(
                "SELECT document_json FROM episode_documents WHERE "
                + " AND ".join(clauses)
                + " ORDER BY "
                "json_extract(document_json, '$.source_position.start'), "
                "episode_ref",
                tuple(parameters),
            ).fetchall()
        return tuple(json.loads(row[0]) for row in rows)

    def resolve_supersession(
        self, enrollment: SourceEnrollment, old_ref: str
    ) -> str | None:
        with self.read_transaction() as connection:
            row = connection.execute(
                "SELECT new_ref FROM supersessions "
                "WHERE corpus_id = ? AND source_id = ? AND old_ref = ? "
                "ORDER BY detected_at DESC, new_ref LIMIT 1",
                (enrollment.corpus_id, enrollment.source_id, old_ref),
            ).fetchone()
        return None if row is None else row[0]
