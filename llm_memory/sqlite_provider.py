from __future__ import annotations

from pathlib import Path

from llm_memory.contract import ProviderCapabilities
from llm_memory.provider import ProviderDescriptor
from llm_memory.sqlite_history import (
    SQLITE_STRATEGY,
    search_history as sqlite_search,
)
from llm_memory.sqlite_lifecycle import (
    measure as sqlite_measure,
    purge as sqlite_purge,
    remove_provider_file,
)
from llm_memory.sqlite_reconcile import reconcile_registry as sqlite_reconcile
from llm_memory.sqlite_store import SQLiteStore


SQLITE_DESCRIPTOR = ProviderDescriptor(
    provider="sqlite",
    implementation_version="1",
    strategies=(SQLITE_STRATEGY,),
    analyzer="porter unicode61 remove_diacritics 2",
    indexed_fields=("user_message", "response", "state_text"),
    match_semantics="analyzed_any_segment_phrase",
    score_ordering="normalized_desc_episode_ref_asc",
    raw_score_polarity="lower_is_better",
)


class SQLiteProvider:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 250):
        self.store = SQLiteStore(path, busy_timeout_ms=busy_timeout_ms)

    def capabilities(self):
        return ProviderCapabilities(
            strategies=SQLITE_DESCRIPTOR.strategies
        ).as_dict() | {"retrieval_basis": SQLITE_DESCRIPTOR.as_dict()}

    def ensure(self):
        return self.store.ensure().as_dict()

    def reconcile(self, registry, budget):
        return sqlite_reconcile(self.store, registry, budget)

    def search(self, registry, request, budget):
        return sqlite_search(self.store, registry, request, budget)

    def resolve_supersession(self, enrollment, old_ref):
        return self.store.resolve_supersession(enrollment, old_ref)

    def purge(self, scope, state_classes):
        return sqlite_purge(self.store, scope, state_classes)

    def remove_all(self):
        return remove_provider_file(self.store)

    def measure(self, scope):
        return sqlite_measure(self.store, scope)
