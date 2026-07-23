import asyncio
import importlib
import json
from uuid import uuid4

import pytest
import yaml

from llm_memory import db as db_module
from llm_memory import mcp_server
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    SOURCE_STATES,
    SUPERSESSIONS,
    ensure_contract_index,
)
from llm_memory.db import get_database
from llm_memory.index import EPISODES, ensure_index
from llm_memory.ingest import ingest_file
from llm_memory.reconcile import ReconcileReport


class RecordingProvider:
    def __init__(self, strategy="selected-provider-strategy"):
        self.strategy = strategy
        self.calls = []
        self.reconciliation = ReconcileReport((), 0, 0.0, False)

    def capabilities(self):
        self.calls.append(("capabilities",))
        return {"strategies": [self.strategy]}

    def ensure(self):
        self.calls.append(("ensure",))
        return {"provider": "recording"}

    def reconcile(self, registry, budget):
        self.calls.append(("reconcile", registry, budget))
        return self.reconciliation

    def search(self, registry, request, budget):
        self.calls.append(("search", registry, request, budget))
        return {"strategy": request.strategy, "registry": registry}

    def resolve_supersession(self, enrollment, old_ref):
        self.calls.append(("resolve", enrollment, old_ref))
        return None


def _run_in_lifespan(operation):
    async def run():
        async with mcp_server.mcp._mcp_server.lifespan(
            mcp_server.mcp._mcp_server
        ) as context:
            return context, operation()

    return asyncio.run(run())


@pytest.fixture(autouse=True)
def isolated_event_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(tmp_path / "events.jsonl"))


@pytest.fixture
def contract_storage():
    db = get_database()
    ensure_contract_index(db)
    prefix = f"mcp-test-{uuid4().hex}"
    try:
        yield db, prefix
    finally:
        for collection_name in (CONTRACT_EPISODES, SOURCE_STATES, SUPERSESSIONS):
            db.aql.execute(
                """
                FOR doc IN @@collection
                    FILTER STARTS_WITH(doc.corpus_id, @prefix)
                    REMOVE doc IN @@collection
                """,
                bind_vars={"@collection": collection_name, "prefix": prefix},
            )


def test_server_exposes_legacy_and_contract_read_tools():
    """The read-only surface keeps legacy tools alongside the episodic contract."""
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert names == {"search", "recall", "search_history", "open_episode"}


def test_search_tool_then_recall_tool_is_a_full_reach(tmp_path):
    """The two tools compose into one reach with no filesystem fallback: the
    search tool returns a hit carrying `key`; the recall tool turns that key into
    the whole episode (longer than the snippet), entirely within the MCP surface."""
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    key = "900050"
    long_response = "the marmoset turnstile marker " + "tail " * 60  # > snippet
    try:
        rec = {
            "cycle": 900050,
            "user_message": "how did the reach go?",
            "raw_output": {"response": long_response},
            "state": {},
        }
        p = tmp_path / "m.jsonl"
        p.write_text(json.dumps(rec))
        ingest_file(db, p)

        hits = mcp_server.search("marmoset turnstile", limit=5)
        hit = next(h for h in hits if h["cycle"] == 900050)
        full = mcp_server.recall(hit["key"])

        assert full["_key"] == key
        assert full["response"] == long_response
    finally:
        if col.has(key):
            col.delete(key)


def test_search_history_then_open_episode_reads_source_backed_content(
    contract_storage, tmp_path, monkeypatch
):
    db, corpus_id = contract_storage
    response_text = "the episodic copper marker " + "source tail " * 20
    source_path = tmp_path / "episodes.jsonl"
    source_path.write_text(
        json.dumps(
            {
                "cycle": 11,
                "timestamp": "2026-07-12T18:30:00Z",
                "model": "test-model",
                "user_message": "where is the copper marker?",
                "response_text": response_text,
                "state": {"topic": "mcp"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "contract_version": 1,
                "sources": [
                    {
                        "corpus_id": corpus_id,
                        "source_id": "taste",
                        "adapter": "taste_open_jsonl",
                        "boundary_version": 1,
                        "canonicalization_version": 1,
                        "locator": str(source_path),
                        "enabled": True,
                        "full_validation_max_age_seconds": 3600,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_MEMORY_SOURCES_CONFIG", str(config_path))
    monkeypatch.delenv("LLM_MEMORY_PROVIDER", raising=False)

    def reach():
        search_response = mcp_server.search_history(
            "episodic copper", [corpus_id], limit=5
        )
        episode_ref = search_response["results"][0]["episode_ref"]
        db.aql.execute(
            """
            FOR doc IN @@episodes
                FILTER doc.corpus_id == @corpus_id
                REMOVE doc IN @@episodes
            """,
            bind_vars={"@episodes": CONTRACT_EPISODES, "corpus_id": corpus_id},
        )
        return search_response, mcp_server.open_episode(episode_ref, [corpus_id])

    _, (search_response, opened) = _run_in_lifespan(reach)

    assert search_response["returned_count"] == 1
    assert opened["standing"] == "available"
    assert opened["response"] == response_text
    assert opened["provenance"]["source_id"] == "taste"
    json.dumps(search_response)
    json.dumps(opened)


def test_missing_config_does_not_prevent_legacy_tools_from_loading(
    tmp_path, monkeypatch
):
    missing_path = tmp_path / "missing-sources.yaml"
    provider = RecordingProvider()
    monkeypatch.setenv("LLM_MEMORY_SOURCES_CONFIG", str(missing_path))
    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)

    context, _ = _run_in_lifespan(lambda: None)
    names = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}

    assert context == {}
    assert {"search", "recall"}.issubset(names)
    assert provider.calls == [("ensure",)]
    with pytest.raises(RuntimeError, match="lifespan is not active"):
        mcp_server.search_history("query", ["configured-corpus"])


def test_service_startup_performs_bounded_reconciliation(monkeypatch):
    registry = object()
    provider = RecordingProvider()

    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)
    monkeypatch.setattr(mcp_server, "load_registry", lambda: registry)

    context, _ = _run_in_lifespan(lambda: None)

    assert context == {"startup_reconciliation": provider.reconciliation}
    assert provider.calls[0] == ("ensure",)
    assert provider.calls[1][0:2] == ("reconcile", registry)
    assert provider.calls[1][2].max_bytes == 1_000_000


def test_service_startup_without_enrollment_config_keeps_legacy_service_available(
    monkeypatch,
):
    provider = RecordingProvider()

    def missing_registry():
        raise FileNotFoundError("no enrollment config")

    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)
    monkeypatch.setattr(mcp_server, "load_registry", missing_registry)

    context, _ = _run_in_lifespan(lambda: None)

    assert context == {}
    assert provider.calls == [("ensure",)]


def test_contract_search_uses_lifespan_provider_registry_and_declared_strategy(
    monkeypatch,
):
    registry = object()
    provider = RecordingProvider("provider-only-strategy")
    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)
    monkeypatch.setattr(mcp_server, "load_registry", lambda: registry)

    _, response = _run_in_lifespan(
        lambda: mcp_server.search_history("query", ["configured-corpus"], limit=7)
    )

    search_call = next(call for call in provider.calls if call[0] == "search")
    assert response == {
        "strategy": "provider-only-strategy",
        "registry": registry,
    }
    assert search_call[1] is registry
    assert search_call[2].strategy == "provider-only-strategy"
    assert search_call[2].limit == 7
    assert search_call[3].max_bytes == 1_000_000


@pytest.mark.parametrize(
    "capabilities",
    [
        None,
        {},
        {"strategies": "provider-strategy"},
        {"strategies": []},
        {"strategies": ["first", "second"]},
        {"strategies": [" "]},
        {"strategies": [1]},
    ],
    ids=(
        "capabilities-wrong-type",
        "strategies-missing",
        "strategies-wrong-type",
        "strategies-empty",
        "strategies-multiple",
        "strategy-blank",
        "strategy-wrong-type",
    ),
)
def test_contract_search_rejects_invalid_sole_strategy_before_request_or_search(
    capabilities, monkeypatch
):
    registry = object()
    provider = RecordingProvider()
    provider.capabilities = lambda: capabilities
    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)
    monkeypatch.setattr(mcp_server, "load_registry", lambda: registry)
    monkeypatch.setattr(
        mcp_server.SearchRequest,
        "create",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid capabilities reached SearchRequest.create")
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="exactly one nonempty string strategy",
    ):
        _run_in_lifespan(
            lambda: mcp_server.search_history("query", ["configured-corpus"])
        )

    assert all(call[0] != "search" for call in provider.calls)


def test_open_episode_uses_only_lifespan_provider_supersession_resolver(monkeypatch):
    registry = object()
    provider = RecordingProvider()
    observed = {}
    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)
    monkeypatch.setattr(mcp_server, "load_registry", lambda: registry)

    def open_source(declared, episode_ref, active_corpus_ids, resolver):
        observed.update(
            registry=declared,
            episode_ref=episode_ref,
            active_corpus_ids=active_corpus_ids,
            resolver=resolver,
        )
        return {"standing": "available"}

    monkeypatch.setattr(mcp_server, "_open_episode", open_source)

    _, response = _run_in_lifespan(
        lambda: mcp_server.open_episode("episode://corpus/session/episode", ["corpus"])
    )

    assert response == {"standing": "available"}
    assert observed["registry"] is registry
    assert observed["episode_ref"] == "episode://corpus/session/episode"
    assert observed["active_corpus_ids"] == ["corpus"]
    assert observed["resolver"].__self__ is provider
    assert observed["resolver"].__func__ is provider.resolve_supersession.__func__


def test_contract_runtime_is_cleared_after_lifespan_exit(monkeypatch):
    monkeypatch.setattr(mcp_server, "load_provider", RecordingProvider)
    monkeypatch.setattr(mcp_server, "load_registry", object)

    _run_in_lifespan(lambda: mcp_server._contract_runtime())

    with pytest.raises(RuntimeError, match="lifespan is not active"):
        mcp_server._contract_runtime()
    with pytest.raises(RuntimeError, match="lifespan is not active"):
        mcp_server.open_episode("episode://corpus/session/episode", ["corpus"])


def test_nested_lifespan_cannot_overwrite_active_runtime(monkeypatch):
    outer_provider = RecordingProvider("outer-strategy")
    inner_provider = RecordingProvider("inner-strategy")
    providers = iter((outer_provider, inner_provider))
    outer_registry = object()
    load_calls = []
    monkeypatch.setattr(
        mcp_server,
        "load_provider",
        lambda: load_calls.append("provider") or next(providers),
    )
    monkeypatch.setattr(mcp_server, "load_registry", lambda: outer_registry)

    async def run():
        async with mcp_server.mcp._mcp_server.lifespan(
            mcp_server.mcp._mcp_server
        ):
            outer_runtime = mcp_server._contract_runtime()
            with pytest.raises(RuntimeError, match="lifespan is already active"):
                async with mcp_server.mcp._mcp_server.lifespan(
                    mcp_server.mcp._mcp_server
                ):
                    pass
            assert mcp_server._contract_runtime() == outer_runtime

    asyncio.run(run())

    assert load_calls == ["provider"]
    assert inner_provider.calls == []


def test_missing_config_inner_lifespan_cannot_observe_outer_runtime(monkeypatch):
    provider = RecordingProvider()
    registry = object()
    registry_loads = []
    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)

    def load_registry_once():
        registry_loads.append("registry")
        if len(registry_loads) == 1:
            return registry
        raise FileNotFoundError("inner enrollment missing")

    monkeypatch.setattr(mcp_server, "load_registry", load_registry_once)

    async def run():
        async with mcp_server.mcp._mcp_server.lifespan(
            mcp_server.mcp._mcp_server
        ):
            with pytest.raises(RuntimeError, match="lifespan is already active"):
                async with mcp_server.mcp._mcp_server.lifespan(
                    mcp_server.mcp._mcp_server
                ):
                    mcp_server._contract_runtime()

    asyncio.run(run())

    assert registry_loads == ["registry"]


def test_failed_lifespan_setup_permits_later_clean_lifespan(monkeypatch):
    failed_provider = RecordingProvider("failed-strategy")
    clean_provider = RecordingProvider("clean-strategy")
    providers = iter((failed_provider, clean_provider))
    clean_registry = object()
    registry_loads = []
    monkeypatch.setattr(mcp_server, "load_provider", lambda: next(providers))

    def load_registry_after_failure():
        registry_loads.append("registry")
        if len(registry_loads) == 1:
            raise ValueError("malformed enrollment config")
        return clean_registry

    monkeypatch.setattr(mcp_server, "load_registry", load_registry_after_failure)

    async def run():
        with pytest.raises(ValueError, match="malformed enrollment config"):
            async with mcp_server.mcp._mcp_server.lifespan(
                mcp_server.mcp._mcp_server
            ):
                pass

        with pytest.raises(RuntimeError, match="lifespan is not active"):
            mcp_server._contract_runtime()

        async with mcp_server.mcp._mcp_server.lifespan(
            mcp_server.mcp._mcp_server
        ):
            assert mcp_server._contract_runtime() == (
                clean_provider,
                clean_registry,
            )

    asyncio.run(run())

    assert registry_loads == ["registry", "registry"]


def test_contract_lifespan_and_tools_call_typed_identifier_only_emitters(
    monkeypatch,
):
    registry = object()
    provider = RecordingProvider()
    provider.reconciliation = ReconcileReport(
        (
            {
                "corpus_id": "codex-history",
                "sources": (
                    {
                        "source_id": "machine-uuid",
                        "members": (
                            {
                                "member_id": "member-1",
                                "episode_count": 2,
                                "source_standing": "available",
                                "index_standing": "available",
                            },
                        ),
                    },
                ),
            },
        ),
        17,
        2.5,
        False,
    )
    search_response = {
        "query": "secret query",
        "returned_count": 1,
        "results": [
            {"episode_ref": "episode://codex-history/session/episode"}
        ],
    }
    opened_response = {
        "episode_ref": "episode://codex-history/session/episode",
        "standing": "available",
        "response": "must not log",
    }
    provider.search = lambda registry, request, budget: search_response
    calls = []

    def record_server(state, *, outcome=None):
        calls.append(("server", state, outcome))
        return False

    def record_reconciliation(**fields):
        calls.append(("reconciliation", fields))
        return False

    def record_search(**fields):
        calls.append(("search", fields))
        return False

    def record_open(**fields):
        calls.append(("open", fields))
        return False

    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)
    monkeypatch.setattr(mcp_server, "load_registry", lambda: registry)
    monkeypatch.setattr(mcp_server, "emit_server_event", record_server)
    monkeypatch.setattr(
        mcp_server, "emit_reconciliation_event", record_reconciliation
    )
    monkeypatch.setattr(mcp_server, "emit_search_event", record_search)
    monkeypatch.setattr(mcp_server, "emit_open_event", record_open)
    monkeypatch.setattr(
        mcp_server, "_open_episode", lambda *args: opened_response
    )

    def operations():
        searched = mcp_server.search_history(
            "secret query", ["codex-history"]
        )
        opened = mcp_server.open_episode(
            "episode://codex-history/session/episode", ["codex-history"]
        )
        return searched, opened

    _, responses = _run_in_lifespan(operations)

    assert responses == (search_response, opened_response)
    assert calls == [
        ("server", "starting", None),
        (
            "reconciliation",
            {
                "corpus_id": "codex-history",
                "source_id": "machine-uuid",
                "member_id": "member-1",
                "source_standing": "available",
                "index_standing": "available",
                "episode_count": 2,
                "bytes_read": 17,
                "duration_ms": 2.5,
                "work_exhausted": False,
            },
        ),
        ("server", "started", None),
        (
            "search",
            {
                "corpus_ids": ["codex-history"],
                "returned_count": 1,
                "episode_refs": [
                    "episode://codex-history/session/episode"
                ],
            },
        ),
        (
            "open",
            {
                "corpus_ids": ["codex-history"],
                "episode_ref": "episode://codex-history/session/episode",
                "standing": "available",
            },
        ),
        ("server", "stopped", None),
    ]
    serialized = json.dumps(calls)
    assert "secret query" not in serialized
    assert "must not log" not in serialized


@pytest.mark.parametrize(
    ("operation", "phase", "corpus_ids", "episode_ref"),
    [
        (
            lambda: mcp_server.search_history(
                "secret query", ["codex-history"]
            ),
            "search",
            ["codex-history"],
            None,
        ),
        (
            lambda: mcp_server.open_episode(
                "episode://codex-history/session/episode",
                ["codex-history"],
            ),
            "open",
            ["codex-history"],
            "episode://codex-history/session/episode",
        ),
    ],
)
def test_contract_failure_calls_typed_emitter_and_reraises_original_exception(
    monkeypatch, operation, phase, corpus_ids, episode_ref
):
    failure = RuntimeError("secret body")
    calls = []

    def record_failure(
        observed_phase, exc, *, corpus_ids=(), episode_ref=None
    ):
        calls.append((observed_phase, exc, corpus_ids, episode_ref))
        return False

    monkeypatch.setattr(
        mcp_server,
        "_contract_runtime",
        lambda: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(mcp_server, "emit_failure_event", record_failure)

    with pytest.raises(RuntimeError, match="secret body") as caught:
        operation()

    assert caught.value is failure
    assert calls == [(phase, failure, corpus_ids, episode_ref)]


def test_failed_startup_calls_typed_failure_and_still_stops(monkeypatch):
    failure = RuntimeError("secret startup body")
    events = []
    provider = RecordingProvider()
    provider.ensure = lambda: (_ for _ in ()).throw(failure)

    def record_server(state, *, outcome=None):
        events.append(("server", state, outcome))
        return False

    def record_failure(phase, exc, *, corpus_ids=(), episode_ref=None):
        events.append(("failure", phase, exc, corpus_ids, episode_ref))
        return False

    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)
    monkeypatch.setattr(mcp_server, "emit_server_event", record_server)
    monkeypatch.setattr(mcp_server, "emit_failure_event", record_failure)

    with pytest.raises(RuntimeError, match="secret startup body") as caught:
        _run_in_lifespan(lambda: None)

    assert caught.value is failure
    assert events == [
        ("server", "starting", None),
        ("failure", "server", failure, (), None),
        ("server", "stopped", None),
    ]


def test_missing_enrollment_emits_started_outcome_and_leaves_runtime_inactive(
    monkeypatch,
):
    events = []
    provider = RecordingProvider()

    def missing_registry():
        raise FileNotFoundError("missing")

    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)
    monkeypatch.setattr(mcp_server, "load_registry", missing_registry)
    monkeypatch.setattr(
        mcp_server,
        "emit_server_event",
        lambda state, *, outcome=None: events.append((state, outcome))
        or False,
    )

    context, _ = _run_in_lifespan(lambda: None)

    assert context == {}
    assert events == [
        ("starting", None),
        ("started", "enrollment_missing"),
        ("stopped", None),
    ]
    with pytest.raises(RuntimeError, match="lifespan is not active"):
        mcp_server._contract_runtime()


def test_malformed_reconciliation_persists_identifier_only_standing(
    tmp_path, monkeypatch
):
    registry = object()
    provider = RecordingProvider()
    provider.reconciliation = ReconcileReport(
        (
            {
                "corpus_id": "codex-history",
                "sources": (
                    {
                        "source_id": "machine-uuid",
                        "members": (
                            {
                                "member_id": "member-1",
                                "episode_count": 0,
                                "source_standing": "malformed",
                                "index_standing": "unavailable",
                            },
                        ),
                    },
                ),
            },
        ),
        9,
        1.0,
        False,
    )
    event_path = tmp_path / "malformed-events.jsonl"
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(event_path))
    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)
    monkeypatch.setattr(mcp_server, "load_registry", lambda: registry)

    _run_in_lifespan(lambda: None)

    records = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
    ]
    reconciliation = next(
        record
        for record in records
        if record["event"] == "reconcile.completed"
    )
    assert reconciliation["source_standing"] == "malformed"
    assert reconciliation["episode_count"] == 0
    assert "body" not in json.dumps(reconciliation)


def test_explicit_sqlite_lifespan_never_connects_to_arango(tmp_path, monkeypatch):
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        yaml.safe_dump({"contract_version": 1, "sources": []}), encoding="utf-8"
    )
    sqlite_path = tmp_path / "episodes.sqlite3"
    monkeypatch.setenv("LLM_MEMORY_PROVIDER", "sqlite")
    monkeypatch.setenv("LLM_MEMORY_SQLITE_PATH", str(sqlite_path))
    monkeypatch.setenv("LLM_MEMORY_SOURCES_CONFIG", str(config_path))
    monkeypatch.setattr(
        "llm_memory.provider_config.get_database",
        lambda: (_ for _ in ()).throw(AssertionError("Arango must stay lazy")),
    )

    context, _ = _run_in_lifespan(lambda: None)

    assert "startup_reconciliation" in context
    assert sqlite_path.exists()


def test_nonempty_sqlite_lifespan_emits_reconciliation_without_arango(
    tmp_path, monkeypatch
):
    source_path = tmp_path / "episodes.jsonl"
    source_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "cycle": cycle,
                    "user_message": question,
                    "response_text": response,
                }
            )
            for cycle, question, response in (
                (1, "first question", "first response"),
                (2, "second question", "second response"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "contract_version": 1,
                "sources": [
                    {
                        "corpus_id": "sqlite-history",
                        "source_id": "sqlite-source",
                        "adapter": "taste_open_jsonl",
                        "boundary_version": 1,
                        "canonicalization_version": 1,
                        "locator": str(source_path),
                        "enabled": True,
                        "full_validation_max_age_seconds": 3600,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    sqlite_path = tmp_path / "episodes.sqlite3"
    event_path = tmp_path / "sqlite-events.jsonl"
    monkeypatch.setenv("LLM_MEMORY_PROVIDER", "sqlite")
    monkeypatch.setenv("LLM_MEMORY_SQLITE_PATH", str(sqlite_path))
    monkeypatch.setenv("LLM_MEMORY_SOURCES_CONFIG", str(config_path))
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(event_path))
    monkeypatch.setattr(
        "llm_memory.provider_config.get_database",
        lambda: (_ for _ in ()).throw(AssertionError("Arango must stay lazy")),
    )

    context, _ = _run_in_lifespan(lambda: None)

    report = context["startup_reconciliation"]
    member = report.corpus_standing[0]["sources"][0]["members"][0]
    assert member["episode_count"] == 2
    events = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
    ]
    reconciliation = next(
        event for event in events if event["event"] == "reconcile.completed"
    )
    assert reconciliation["episode_count"] == 2
    assert sqlite_path.exists()


def test_legacy_tools_acquire_arango_database_lazily(monkeypatch):
    database = object()
    calls = []
    monkeypatch.setattr(
        mcp_server,
        "get_database",
        lambda: calls.append("get_database") or database,
    )
    monkeypatch.setattr(
        mcp_server,
        "_search",
        lambda db, query, *, scope, limit: (db, query, scope, limit),
    )
    monkeypatch.setattr(mcp_server, "_recall", lambda db, key: (db, key))

    assert calls == []
    assert mcp_server.search("needle", scope="scope", limit=3) == (
        database,
        "needle",
        "scope",
        3,
    )
    assert mcp_server.recall("episode-key") == (database, "episode-key")
    assert calls == ["get_database", "get_database"]


def test_import_does_not_connect_to_arango(monkeypatch):
    with monkeypatch.context() as isolated:
        isolated.setattr(
            db_module,
            "get_database",
            lambda: (_ for _ in ()).throw(
                AssertionError("module import must not connect to Arango")
            ),
        )
        importlib.reload(mcp_server)

    importlib.reload(mcp_server)
