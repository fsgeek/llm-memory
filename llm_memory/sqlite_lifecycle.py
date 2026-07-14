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
    paths = {
        "database": store.path,
        "wal": Path(f"{store.path}-wal"),
        "shm": Path(f"{store.path}-shm"),
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


def remove_provider_file(store: SQLiteStore) -> dict[str, object]:
    candidates = (
        store.path,
        Path(f"{store.path}-wal"),
        Path(f"{store.path}-shm"),
    )

    operation_connection = None
    if store.path.exists():
        try:
            operation_connection = store.connect()
        except (sqlite3.Error, ProviderUnavailable, ProviderUnsupported, OSError):
            pass
        finally:
            if operation_connection is not None:
                operation_connection.close()

    removed = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except FileNotFoundError:
            pass
        except OSError:
            continue
    residual = [path.name for path in candidates if path.exists()]
    return {
        "removed_paths": removed,
        "residual_paths": residual,
        "declared_losses": [
            "retained supersession observations",
            "non-reproducible evaluation state",
        ],
        "retained": ["enrollment configuration", "source locators"],
    }
