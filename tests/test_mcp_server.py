import asyncio
import json
from uuid import uuid4

import pytest

from llm_memory import mcp_server
from llm_memory.db import get_database
from llm_memory.index import EPISODES, ensure_index
from llm_memory.ingest import ingest_file


@pytest.fixture(autouse=True)
def isolated_event_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(tmp_path / "events.jsonl"))


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
        lambda db, query, *, scope, limit, since, until: (
            db,
            query,
            scope,
            limit,
            since,
            until,
        ),
    )
    monkeypatch.setattr(mcp_server, "_recall", lambda db, key: (db, key))

    assert calls == []
    assert mcp_server.search("needle", scope="scope", limit=3) == (
        database,
        "needle",
        "scope",
        3,
        None,
        None,
    )
    assert mcp_server.recall("episode-key") == (database, "episode-key")
    assert calls == ["get_database", "get_database"]
