from __future__ import annotations

from llm_memory.contract import ProviderCapabilities, STRATEGY
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    CONTRACT_VIEW,
    SOURCE_STATES,
    SUPERSESSIONS,
    ensure_contract_index,
)
from llm_memory.history import (
    _replacement_ref as arango_replacement_ref,
    search_history as arango_search,
)
from llm_memory.lifecycle import purge_derived
from llm_memory.provider import ProviderDescriptor, ProviderMeasurement, PurgeScope
from llm_memory.reconcile import reconcile_registry


ARANGO_DESCRIPTOR = ProviderDescriptor(
    provider="arango",
    implementation_version="1",
    strategies=(STRATEGY,),
    analyzer="text_en",
    indexed_fields=("user_message", "response", "state_text"),
    match_semantics="analyzed_any_token",
    score_ordering="higher_is_better",
    raw_score_polarity="higher_is_better",
)

_DERIVED_COLLECTIONS = {
    "episodes": CONTRACT_EPISODES,
    "reconciliation": SOURCE_STATES,
    "supersessions": SUPERSESSIONS,
}
_MEASUREMENT_COLLECTIONS = {
    "episode_documents": CONTRACT_EPISODES,
    "source_state_documents": SOURCE_STATES,
    "supersession_documents": SUPERSESSIONS,
}


def _validated_classes(classes: frozenset[str]) -> frozenset[str]:
    if (
        not isinstance(classes, frozenset)
        or not classes
        or not classes <= _DERIVED_COLLECTIONS.keys()
    ):
        raise ValueError(
            "classes must be a non-empty frozenset containing only "
            f"{sorted(_DERIVED_COLLECTIONS)!r}"
        )
    return classes


def purge_derived_scope(
    db,
    scope: PurgeScope,
    *,
    classes: frozenset[str],
) -> dict[str, int]:
    if scope.corpus_id is not None:
        return purge_derived(
            db,
            scope.corpus_id,
            scope.source_id,
            classes=classes,
        )

    report = {}
    for derived_class in sorted(_validated_classes(classes)):
        removed = list(
            db.aql.execute(
                """
                FOR doc IN @@collection
                    REMOVE doc IN @@collection
                    RETURN OLD._key
                """,
                bind_vars={
                    "@collection": _DERIVED_COLLECTIONS[derived_class],
                },
            )
        )
        report[derived_class] = len(removed)
    return report


def remove_arango_contract_state(db) -> dict[str, object]:
    removed = []
    if CONTRACT_VIEW in {view["name"] for view in db.views()}:
        db.delete_view(CONTRACT_VIEW)
        removed.append(CONTRACT_VIEW)
    for name in (CONTRACT_EPISODES, SOURCE_STATES, SUPERSESSIONS):
        if db.has_collection(name):
            db.delete_collection(name)
            removed.append(name)
    return {
        "removed_objects": removed,
        "declared_losses": ["retained supersession observations"],
    }


def _document_count(db, collection_name: str, scope: PurgeScope) -> int:
    if scope.corpus_id is None:
        return db.collection(collection_name).count()
    return list(
        db.aql.execute(
            """
            RETURN LENGTH(
                FOR doc IN @@collection
                    FILTER doc.corpus_id == @corpus_id
                    FILTER @source_id == null OR doc.source_id == @source_id
                    RETURN 1
            )
            """,
            bind_vars={
                "@collection": collection_name,
                "corpus_id": scope.corpus_id,
                "source_id": scope.source_id,
            },
        )
    )[0]


class ArangoProvider:
    def __init__(self, db):
        self._db = db

    def capabilities(self) -> dict[str, object]:
        return ProviderCapabilities(
            strategies=ARANGO_DESCRIPTOR.strategies
        ).as_dict() | {"retrieval_basis": ARANGO_DESCRIPTOR.as_dict()}

    def ensure(self) -> dict[str, object]:
        ensure_contract_index(self._db)
        return {"provider": "arango", "index_standing": "available"}

    def reconcile(self, registry, budget):
        return reconcile_registry(self._db, registry, budget)

    def search(self, registry, request, budget):
        return arango_search(self._db, registry, request, budget)

    def resolve_supersession(self, enrollment, old_ref):
        return arango_replacement_ref(self._db, enrollment, old_ref)

    def purge(self, scope, state_classes):
        return purge_derived_scope(self._db, scope, classes=state_classes)

    def remove_all(self):
        return remove_arango_contract_state(self._db)

    def measure(self, scope: PurgeScope) -> ProviderMeasurement:
        collection_names = set(_MEASUREMENT_COLLECTIONS.values())
        available = all(
            self._db.has_collection(name) for name in collection_names
        ) and CONTRACT_VIEW in {view["name"] for view in self._db.views()}
        observations: dict[str, int | float | str | None] = {
            observation: (
                _document_count(self._db, collection_name, scope)
                if self._db.has_collection(collection_name)
                else None
            )
            for observation, collection_name in _MEASUREMENT_COLLECTIONS.items()
        }
        return ProviderMeasurement(
            provider="arango",
            standing="available" if available else "unavailable",
            observations=observations,
        )
