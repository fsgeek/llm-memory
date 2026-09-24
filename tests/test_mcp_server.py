import asyncio
import hashlib
import hmac
import json
from uuid import uuid4

import pytest

from khipumaq import mcp_server
from khipumaq.db import get_database
from khipumaq.index import EPISODES, ensure_index
from khipumaq.ingest import ingest_file


@pytest.fixture(autouse=True)
def isolated_event_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(tmp_path / "events.jsonl"))


def test_server_exposes_only_the_read_tools():
    names = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}

    assert names == {"search", "recall", "describe"}


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

        hits = mcp_server.search("marmoset turnstile", limit=5)["hits"]
        hit = next(h for h in hits if h["cycle"] == 900050)
        full = mcp_server.recall(hit["key"])

        assert full["_key"] == key
        assert full["response"] == long_response
    finally:
        if col.has(key):
            col.delete(key)


def test_search_tool_returns_envelope_and_honors_time_window(tmp_path):
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    marker = f"mcpwindowmarker{uuid4().hex}"
    first_cycle = 980_000_000 + (uuid4().int % 10_000_000)
    cycles = [first_cycle + offset for offset in range(3)]
    keys = [str(cycle) for cycle in cycles]
    since = "2026-08-10T00:00:00Z"
    until = "2026-08-20T00:00:00Z"
    timestamps = ("2026-08-09T23:59:59Z", since, until)
    try:
        records = [
            {
                "cycle": cycle,
                "timestamp": timestamp,
                "user_message": "MCP time-window search",
                "raw_output": {"response": f"shared search marker {marker}"},
                "state": {},
            }
            for cycle, timestamp in zip(cycles, timestamps)
        ]
        path = tmp_path / "mcp-window.jsonl"
        path.write_text("\n".join(json.dumps(record) for record in records))
        ingest_file(db, path)

        result = mcp_server.search(marker, since=since, until=until)

        assert isinstance(result, dict)
        assert set(result) == {"total", "hits"}
        assert result["total"] == 1
        assert [hit["cycle"] for hit in result["hits"]] == [cycles[1]]
    finally:
        for key in keys:
            if col.has(key):
                col.delete(key)


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
        lambda db, query, *, scope, limit, since, until: calls.append(
            ("search", db, query, scope, limit, since, until)
        )
        or {"total": 1, "hits": [{"key": "search-result"}]},
    )
    monkeypatch.setattr(mcp_server, "_recall", lambda db, key: (db, key))

    assert calls == []
    assert mcp_server.search(
        "needle",
        scope="scope",
        limit=3,
        since="2026-08-01T00:00:00Z",
        until="2026-08-31T23:59:59Z",
    ) == {"total": 1, "hits": [{"key": "search-result"}]}
    assert mcp_server.recall("episode-key") == (database, "episode-key")
    assert calls == [
        "get_database",
        (
            "search",
            database,
            "needle",
            "scope",
            3,
            "2026-08-01T00:00:00Z",
            "2026-08-31T23:59:59Z",
        ),
        "get_database",
    ]


def test_search_tool_emits_a_content_free_completed_event(tmp_path, monkeypatch):
    database = object()
    query = "private capybara query"
    snippet = "private snippet from an episode"
    result = {
        "total": 7,
        "hits": [
            {"key": "episode-a", "snippet": snippet},
            {"key": "episode-b", "snippet": "another private snippet"},
        ],
    }
    search_calls = []
    monkeypatch.setattr(mcp_server, "get_database", lambda: database)
    monkeypatch.setattr(
        mcp_server,
        "_search",
        lambda db, search_query, *, scope, limit, since, until: search_calls.append(
            (db, search_query, scope, limit, since, until)
        )
        or result,
    )

    returned = mcp_server.search(
        query,
        scope="hamutay",
        limit=2,
        since="2026-08-01T00:00:00Z",
        until="2026-09-01T00:00:00Z",
    )

    event_text = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in event_text.splitlines()]
    key = (tmp_path / "event-key").read_bytes()
    assert returned is result
    assert search_calls == [
        (
            database,
            query,
            "hamutay",
            2,
            "2026-08-01T00:00:00Z",
            "2026-09-01T00:00:00Z",
        )
    ]
    assert len(records) == 1
    assert records[0] == {
        "event": "search.completed",
        "keys_sha256": hashlib.sha256(b"episode-a\nepisode-b").hexdigest(),
        "query_hmac": hmac.new(
            key, query.encode("utf-8"), hashlib.sha256
        ).hexdigest(),
        "returned": 2,
        "scope": "hamutay",
        "since": "2026-08-01T00:00:00Z",
        "total": 7,
        "ts": records[0]["ts"],
        "until": "2026-09-01T00:00:00Z",
    }
    assert query not in event_text
    assert snippet not in event_text


def test_recall_tool_emits_a_completed_event(tmp_path, monkeypatch):
    database = object()
    episode = {"_key": "episode-a", "response": "private episode body"}
    monkeypatch.setattr(mcp_server, "get_database", lambda: database)
    monkeypatch.setattr(
        mcp_server,
        "_recall",
        lambda db, key: episode if (db, key) == (database, "episode-a") else None,
    )

    returned = mcp_server.recall("episode-a")

    event_text = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in event_text.splitlines()]
    assert returned is episode
    assert len(records) == 1
    assert records[0] == {
        "event": "recall.completed",
        "found": True,
        "key": "episode-a",
        "ts": records[0]["ts"],
    }
    assert episode["response"] not in event_text
