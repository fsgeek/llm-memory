from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    SOURCE_STATES,
    SUPERSESSIONS,
)
from llm_memory.enrollment import load_registry


_DERIVED_COLLECTIONS = {
    "episodes": CONTRACT_EPISODES,
    "reconciliation": SOURCE_STATES,
    "supersessions": SUPERSESSIONS,
}
_DERIVED_CLASSES = frozenset(_DERIVED_COLLECTIONS)


def _validated_config(path: Path) -> dict[str, Any]:
    load_registry(path)
    with path.open(encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def _source_index(config: dict[str, Any], corpus_id: str, source_id: str) -> int:
    for index, source in enumerate(config["sources"]):
        if source["corpus_id"] == corpus_id and source["source_id"] == source_id:
            return index
    raise ValueError(f"source is not enrolled: {corpus_id!r}, {source_id!r}")


def _atomic_write_config(path: Path, config: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
            yaml.safe_dump(config, config_file, sort_keys=False)
            config_file.flush()
            os.fsync(config_file.fileno())
        os.chmod(temporary, path.stat().st_mode)
        load_registry(temporary)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _operation_report(
    operation: str,
    corpus_id: str,
    source_id: str,
    *,
    changed: bool,
) -> dict:
    return {
        "operation": operation,
        "corpus_id": corpus_id,
        "source_id": source_id,
        "changed": changed,
    }


def disable_source(config_path: Path, corpus_id: str, source_id: str) -> dict:
    path = Path(config_path)
    config = _validated_config(path)
    index = _source_index(config, corpus_id, source_id)
    changed = config["sources"][index]["enabled"] is not False
    if changed:
        config["sources"][index]["enabled"] = False
        _atomic_write_config(path, config)
    return _operation_report("disable", corpus_id, source_id, changed=changed)


def unenroll_source(config_path: Path, corpus_id: str, source_id: str) -> dict:
    path = Path(config_path)
    config = _validated_config(path)
    index = _source_index(config, corpus_id, source_id)
    del config["sources"][index]
    _atomic_write_config(path, config)
    return _operation_report("unenroll", corpus_id, source_id, changed=True)


def _require_identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or not value or "/" in value:
        raise ValueError(f"{name} must be non-empty and contain no '/'")


def purge_derived(
    db,
    corpus_id: str,
    source_id: str | None = None,
    *,
    classes: frozenset[str],
) -> dict[str, int]:
    _require_identifier("corpus_id", corpus_id)
    if source_id is not None:
        _require_identifier("source_id", source_id)
    if (
        not isinstance(classes, frozenset)
        or not classes
        or not classes <= _DERIVED_CLASSES
    ):
        raise ValueError(
            "classes must be a non-empty frozenset containing only "
            f"{sorted(_DERIVED_CLASSES)!r}"
        )

    report = {}
    for derived_class in sorted(classes):
        removed = list(
            db.aql.execute(
                """
                FOR doc IN @@collection
                    FILTER doc.corpus_id == @corpus_id
                    FILTER @source_id == null OR doc.source_id == @source_id
                    REMOVE doc IN @@collection
                    RETURN OLD._key
                """,
                bind_vars={
                    "@collection": _DERIVED_COLLECTIONS[derived_class],
                    "corpus_id": corpus_id,
                    "source_id": source_id,
                },
            )
        )
        report[derived_class] = len(removed)
    return report
