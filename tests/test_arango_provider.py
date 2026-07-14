from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from llm_memory.arango_provider import (
    ARANGO_DESCRIPTOR,
    ArangoProvider,
    purge_derived_scope,
    remove_arango_contract_state,
)
from llm_memory.contract import SearchRequest
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    CONTRACT_VIEW,
    SOURCE_STATES,
    SUPERSESSIONS,
)
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.provider import PurgeScope
from llm_memory.reconcile import ReconcileReport, WorkBudget


class FakeAQL:
    def __init__(self, removals: dict[str, list[str]] | None = None):
        self.removals = removals or {}
        self.calls = []

    def execute(self, query, *, bind_vars):
        self.calls.append((query, bind_vars))
        return self.removals.get(bind_vars["@collection"], [])


class FakeCollection:
    def __init__(self, count: int):
        self._count = count

    def count(self):
        return self._count


class FakeDB:
    def __init__(
        self,
        *,
        collections: dict[str, int] | None = None,
        views: tuple[str, ...] = (),
        removals: dict[str, list[str]] | None = None,
    ):
        self._collections = dict(collections or {})
        self._views = list(views)
        self.aql = FakeAQL(removals)
        self.deleted_views = []
        self.deleted_collections = []

    def views(self):
        return [{"name": name} for name in self._views]

    def has_collection(self, name):
        return name in self._collections

    def collection(self, name):
        return FakeCollection(self._collections[name])

    def delete_view(self, name):
        self.deleted_views.append(name)
        self._views.remove(name)

    def delete_collection(self, name):
        self.deleted_collections.append(name)
        del self._collections[name]


def enrollment() -> SourceEnrollment:
    return SourceEnrollment(
        corpus_id="local",
        source_id="source-a",
        adapter="gateway_jsonl",
        boundary_version=1,
        canonicalization_version=1,
        locator=Path("/unused"),
        enabled=True,
        full_validation_max_age_seconds=3600,
    )


def budget() -> WorkBudget:
    return WorkBudget(100, datetime(2026, 7, 14, tzinfo=UTC))


def test_arango_descriptor_preserves_stage_one_retrieval_basis():
    assert ARANGO_DESCRIPTOR.as_dict() == {
        "provider": "arango",
        "implementation_version": "1",
        "strategies": ("lexical_bm25_text_en_v1",),
        "analyzer": "text_en",
        "indexed_fields": ("user_message", "response", "state_text"),
        "match_semantics": "analyzed_any_token",
        "score_ordering": "higher_is_better",
        "raw_score_polarity": "higher_is_better",
    }


def test_arango_provider_capabilities_include_retrieval_basis():
    capabilities = ArangoProvider(object()).capabilities()

    assert capabilities == {
        "contract_versions": [1],
        "strategies": ["lexical_bm25_text_en_v1"],
        "supports_facets": False,
        "supports_continuation": False,
        "max_limit": 100,
        "retrieval_basis": ARANGO_DESCRIPTOR.as_dict(),
    }


def test_arango_provider_delegates_search_without_changing_request(monkeypatch):
    observed = {}
    db = object()
    registry = EnrollmentRegistry((enrollment(),))
    work = budget()
    monkeypatch.setattr(
        "llm_memory.arango_provider.arango_search",
        lambda actual_db, reg, req, actual_work: observed.update(
            db=actual_db,
            registry=reg,
            request=req,
            budget=actual_work,
        )
        or {"results": []},
    )
    provider = ArangoProvider(db)
    request = SearchRequest.create("reason", ["local"])

    assert provider.search(registry, request, work) == {"results": []}
    assert observed == {
        "db": db,
        "registry": registry,
        "request": request,
        "budget": work,
    }
    assert observed["request"] is request


def test_arango_provider_delegates_ensure_reconcile_and_supersession(monkeypatch):
    calls = []
    db = object()
    source = enrollment()
    registry = EnrollmentRegistry((source,))
    work = budget()
    report = ReconcileReport((), 0, 0.0, False)
    monkeypatch.setattr(
        "llm_memory.arango_provider.ensure_contract_index",
        lambda actual_db: calls.append(("ensure", actual_db)),
    )
    monkeypatch.setattr(
        "llm_memory.arango_provider.reconcile_registry",
        lambda actual_db, reg, actual_work: calls.append(
            ("reconcile", actual_db, reg, actual_work)
        )
        or report,
    )
    monkeypatch.setattr(
        "llm_memory.arango_provider.arango_replacement_ref",
        lambda actual_db, actual_source, old_ref: calls.append(
            ("resolve", actual_db, actual_source, old_ref)
        )
        or "episode://local/new/ref",
    )
    provider = ArangoProvider(db)

    assert provider.ensure() == {"provider": "arango", "index_standing": "available"}
    assert provider.reconcile(registry, work) is report
    assert provider.resolve_supersession(source, "episode://local/old/ref") == (
        "episode://local/new/ref"
    )
    assert calls == [
        ("ensure", db),
        ("reconcile", db, registry, work),
        ("resolve", db, source, "episode://local/old/ref"),
    ]


def test_arango_provider_delegates_purge_and_remove_all(monkeypatch):
    calls = []
    db = object()
    scope = PurgeScope(corpus_id="local", source_id="source-a")
    classes = frozenset({"episodes", "supersessions"})
    monkeypatch.setattr(
        "llm_memory.arango_provider.purge_derived_scope",
        lambda actual_db, actual_scope, *, classes: calls.append(
            ("purge", actual_db, actual_scope, classes)
        )
        or {"episodes": 2, "supersessions": 1},
    )
    monkeypatch.setattr(
        "llm_memory.arango_provider.remove_arango_contract_state",
        lambda actual_db: calls.append(("remove", actual_db))
        or {"removed_objects": []},
    )
    provider = ArangoProvider(db)

    assert provider.purge(scope, classes) == {"episodes": 2, "supersessions": 1}
    assert provider.remove_all() == {"removed_objects": []}
    assert calls == [
        ("purge", db, scope, classes),
        ("remove", db),
    ]


def test_purge_derived_scope_delegates_corpus_and_source_scope(monkeypatch):
    calls = []
    db = object()
    classes = frozenset({"episodes"})
    monkeypatch.setattr(
        "llm_memory.arango_provider.purge_derived",
        lambda actual_db, corpus_id, source_id=None, *, classes: calls.append(
            (actual_db, corpus_id, source_id, classes)
        )
        or {"episodes": 1},
    )

    assert purge_derived_scope(
        db, PurgeScope(corpus_id="local"), classes=classes
    ) == {"episodes": 1}
    assert purge_derived_scope(
        db,
        PurgeScope(corpus_id="local", source_id="source-a"),
        classes=classes,
    ) == {"episodes": 1}
    assert calls == [
        (db, "local", None, classes),
        (db, "local", "source-a", classes),
    ]


def test_purge_derived_scope_removes_global_derived_state_only():
    db = FakeDB(
        removals={
            CONTRACT_EPISODES: ["episode-a", "episode-b"],
            SUPERSESSIONS: ["supersession-a"],
        }
    )

    assert purge_derived_scope(
        db,
        PurgeScope(),
        classes=frozenset({"episodes", "supersessions"}),
    ) == {"episodes": 2, "supersessions": 1}
    assert [call[1]["@collection"] for call in db.aql.calls] == [
        CONTRACT_EPISODES,
        SUPERSESSIONS,
    ]
    assert all("corpus_id" not in query for query, _ in db.aql.calls)


def test_arango_measure_reports_document_counts_without_disk_size_claims():
    db = FakeDB(
        collections={
            CONTRACT_EPISODES: 12,
            SOURCE_STATES: 3,
            SUPERSESSIONS: 2,
        },
        views=(CONTRACT_VIEW,),
    )

    measurement = ArangoProvider(db).measure(PurgeScope())

    assert measurement.provider == "arango"
    assert measurement.standing == "available"
    assert measurement.observations == {
        "episode_documents": 12,
        "source_state_documents": 3,
        "supersession_documents": 2,
    }
    assert all("byte" not in key and "disk" not in key for key in measurement.observations)


def test_remove_arango_contract_state_drops_only_owned_objects():
    db = FakeDB(
        collections={
            CONTRACT_EPISODES: 12,
            SOURCE_STATES: 3,
            SUPERSESSIONS: 2,
            "unrelated_collection": 99,
        },
        views=(CONTRACT_VIEW, "unrelated_view"),
    )

    assert remove_arango_contract_state(db) == {
        "removed_objects": [
            CONTRACT_VIEW,
            CONTRACT_EPISODES,
            SOURCE_STATES,
            SUPERSESSIONS,
        ],
        "declared_losses": ["retained supersession observations"],
    }
    assert db.deleted_views == [CONTRACT_VIEW]
    assert db.deleted_collections == [
        CONTRACT_EPISODES,
        SOURCE_STATES,
        SUPERSESSIONS,
    ]
    assert db.views() == [{"name": "unrelated_view"}]
    assert db.has_collection("unrelated_collection")
