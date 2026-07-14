from __future__ import annotations

import sqlite3
from pathlib import Path

from llm_memory.provider import (
    ProviderMeasurement,
    ProviderUnavailable,
    ProviderUnsupported,
    PurgeScope,
)
from llm_memory.sqlite_store import SQLiteStore


DERIVED_CLASSES = frozenset({"episodes", "reconciliation", "supersessions"})

_DERIVED_TABLES = {
    "episodes": "episode_documents",
    "reconciliation": "source_states",
    "supersessions": "supersessions",
}
_ROW_COUNT_NAMES = (
    "episode_documents",
    "episode_fts_rows",
    "source_state_documents",
    "supersession_documents",
)
_PROVIDER_TABLES = (
    "episode_fts",
    "episode_documents",
    "source_states",
    "supersessions",
    "provider_meta",
)
_ACTIVE_SNAPSHOT_RESIDUAL = "active SQLite snapshot prevents verified removal"
_INVALIDATION_RESIDUAL = "SQLite provider content invalidation did not complete"


def _require_identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or not value or "/" in value:
        raise ValueError(f"{name} must be non-empty and contain no '/'")


def _validated_scope(scope: PurgeScope) -> PurgeScope:
    if not isinstance(scope, PurgeScope):
        raise TypeError("scope must be a PurgeScope")
    if scope.corpus_id is not None:
        _require_identifier("corpus_id", scope.corpus_id)
    if scope.source_id is not None:
        _require_identifier("source_id", scope.source_id)
    return scope


def _validated_classes(state_classes: frozenset[str]) -> frozenset[str]:
    if (
        not isinstance(state_classes, frozenset)
        or not state_classes
        or not state_classes <= DERIVED_CLASSES
    ):
        raise ValueError(
            "state_classes must be a non-empty frozenset containing only "
            f"{sorted(DERIVED_CLASSES)!r}"
        )
    return state_classes


def _scope_predicate(
    scope: PurgeScope, *, qualifier: str = ""
) -> tuple[str, tuple[str, ...]]:
    if scope.corpus_id is None:
        return "", ()
    corpus_id = f"{qualifier}corpus_id"
    source_id = f"{qualifier}source_id"
    if scope.source_id is None:
        return f" WHERE {corpus_id} = ?", (scope.corpus_id,)
    return (
        f" WHERE {corpus_id} = ? AND {source_id} = ?",
        (scope.corpus_id, scope.source_id),
    )


def purge(
    store: SQLiteStore,
    scope: PurgeScope,
    state_classes: frozenset[str],
) -> dict[str, int]:
    validated_scope = _validated_scope(scope)
    validated_classes = _validated_classes(state_classes)
    predicate, parameters = _scope_predicate(validated_scope)
    with store.write_transaction() as connection:
        counts = {}
        for state_class in sorted(validated_classes):
            cursor = connection.execute(
                f"DELETE FROM {_DERIVED_TABLES[state_class]}{predicate}",
                parameters,
            )
            counts[state_class] = cursor.rowcount
        if (
            "episodes" in validated_classes
            and "reconciliation" not in validated_classes
        ):
            active_predicate = f"{predicate} AND" if predicate else " WHERE"
            connection.execute(
                "UPDATE source_states SET revision = revision + 1, "
                "state_json = json_set("
                "state_json, "
                "'$.active_generation_integrity', "
                "CASE WHEN json_extract(state_json, '$.active_generation_id') "
                "IS NOT NULL THEN 'invalid' ELSE NULL END, "
                "'$.freshness', "
                "CASE WHEN json_extract(state_json, '$.active_generation_id') "
                "IS NOT NULL THEN 'stale' ELSE 'incomplete' END, "
                "'$.staging_generation_id', NULL, "
                "'$.staging_episode_count', NULL, "
                "'$.staging_canonicalization_version', NULL, "
                "'$.staging_boundary_version', NULL, "
                "'$.build_generation_id', NULL, "
                "'$.build_mode', NULL, "
                "'$.build_reason', NULL, "
                "'$.build_cursor', NULL, "
                "'$.build_chain_digest', NULL, "
                "'$.build_seeded', NULL, "
                "'$.build_canonicalization_version', NULL, "
                "'$.build_boundary_version', NULL, "
                "'$.build_source_snapshot', NULL, "
                "'$.build_observed_end', NULL, "
                "'$.build_complete_end', NULL, "
                "'$.build_bytes_read', NULL, "
                "'$.build_elapsed_ms', NULL"
                ")"
                f"{active_predicate} "
                "(json_extract(state_json, '$.active_generation_id') IS NOT NULL "
                "OR json_extract(state_json, '$.staging_generation_id') IS NOT NULL "
                "OR json_extract(state_json, '$.build_generation_id') IS NOT NULL)",
                parameters,
            )
    return counts


def _physical_observation(path: Path) -> tuple[int, str]:
    try:
        return path.stat().st_size, "available"
    except FileNotFoundError:
        return 0, "absent"
    except OSError:
        return 0, "unavailable"


def _empty_row_counts() -> dict[str, int | str | None]:
    return {"query_standing": "unavailable"} | {
        name: None for name in _ROW_COUNT_NAMES
    }


def _row_counts(store: SQLiteStore, scope: PurgeScope) -> dict[str, int | str | None]:
    predicate, parameters = _scope_predicate(scope)
    try:
        with store.read_transaction() as connection:
            episodes = connection.execute(
                f"SELECT count(*) FROM episode_documents{predicate}", parameters
            ).fetchone()[0]
            states = connection.execute(
                f"SELECT count(*) FROM source_states{predicate}", parameters
            ).fetchone()[0]
            supersessions = connection.execute(
                f"SELECT count(*) FROM supersessions{predicate}", parameters
            ).fetchone()[0]
            episode_predicate, episode_parameters = _scope_predicate(
                scope, qualifier="episode."
            )
            fts = connection.execute(
                "SELECT count(*) FROM episode_fts AS fts "
                "JOIN episode_documents AS episode ON episode.rowid = fts.rowid"
                + episode_predicate,
                episode_parameters,
            ).fetchone()[0]
    except (sqlite3.Error, ProviderUnavailable, ProviderUnsupported, OSError):
        return _empty_row_counts()
    return {
        "query_standing": "available",
        "episode_documents": episodes,
        "episode_fts_rows": fts,
        "source_state_documents": states,
        "supersession_documents": supersessions,
    }


def measure(store: SQLiteStore, scope: PurgeScope) -> ProviderMeasurement:
    validated_scope = _validated_scope(scope)
    store.validate_path()
    database, wal, shm = store.file_paths()
    paths = {
        "database": database,
        "wal": wal,
        "shm": shm,
    }
    physical = {name: _physical_observation(path) for name, path in paths.items()}
    rows = (
        _row_counts(store, validated_scope)
        if physical["database"][1] == "available"
        else _empty_row_counts()
    )
    available = (
        physical["database"][1] == "available"
        and rows["query_standing"] == "available"
    )
    scope_name = (
        "global"
        if validated_scope.corpus_id is None
        else "corpus"
        if validated_scope.source_id is None
        else "source"
    )
    observations: dict[str, int | float | str | None] = {
        "scope": scope_name,
        "corpus_id": validated_scope.corpus_id,
        "source_id": validated_scope.source_id,
        "fts_representation": "self_contained_duplicate",
    }
    for name, (byte_count, standing) in physical.items():
        observations[f"{name}_bytes"] = byte_count
        observations[f"{name}_stat_standing"] = standing
    observations.update(rows)
    return ProviderMeasurement(
        provider="sqlite",
        standing="available" if available else "unavailable",
        observations=observations,
    )


def _symlink_residual_reason(
    name: str, database_name: str, symlinks: frozenset[str]
) -> str:
    if name in symlinks:
        if name == database_name:
            return "configured SQLite database path is a symlink"
        return "configured SQLite companion path is a symlink"
    if database_name in symlinks:
        return "not removed because configured SQLite database path is a symlink"
    return "not removed because SQLite provider file set contains a symlink"


def _invalidate_provider_content(store: SQLiteStore) -> str | None:
    connection = None
    try:
        connection = store.connect()
        connection.execute("BEGIN IMMEDIATE")
        for table in _PROVIDER_TABLES:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()
        busy = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        if busy:
            return _ACTIVE_SNAPSHOT_RESIDUAL
        return None
    except (sqlite3.Error, ProviderUnavailable, ProviderUnsupported, OSError):
        if connection is not None and connection.in_transaction:
            connection.rollback()
        return _INVALIDATION_RESIDUAL
    finally:
        if connection is not None:
            connection.close()


def remove_provider_file(store: SQLiteStore) -> dict[str, object]:
    candidates = store.file_paths()
    symlinks = frozenset(path.name for path in candidates if path.is_symlink())
    if symlinks:
        residual = [path.name for path in candidates if _path_present(path)]
        return {
            "removed_paths": [],
            "residual_paths": residual,
            "residual_reasons": {
                name: _symlink_residual_reason(name, store.path.name, symlinks)
                for name in residual
            },
            "declared_losses": [
                "retained supersession observations",
                "non-reproducible evaluation state",
            ],
            "retained": ["enrollment configuration", "source locators"],
        }

    if _path_present(store.path):
        invalidation_residual = _invalidate_provider_content(store)
        if invalidation_residual is not None:
            residual = [path.name for path in candidates if _path_present(path)]
            return {
                "removed_paths": [],
                "residual_paths": residual,
                "residual_reasons": {
                    name: invalidation_residual for name in residual
                },
                "declared_losses": [
                    "retained supersession observations",
                    "non-reproducible evaluation state",
                ],
                "retained": ["enrollment configuration", "source locators"],
            }

    removed = []
    removal_failures = {}
    for path in candidates:
        if not _path_present(path):
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            removal_failures[path.name] = str(exc)
            continue
    residual = [path.name for path in candidates if _path_present(path)]
    return {
        "removed_paths": removed,
        "residual_paths": residual,
        "residual_reasons": {
            name: removal_failures[name]
            for name in residual
            if name in removal_failures
        },
        "declared_losses": [
            "retained supersession observations",
            "non-reproducible evaluation state",
        ],
        "retained": ["enrollment configuration", "source locators"],
    }


def _path_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True
