import asyncio
import json

from llm_memory.db import get_database
from llm_memory.index import EPISODES, ensure_index
from llm_memory.ingest import ingest_file
from llm_memory import mcp_server


def test_server_exposes_only_read_tools():
    """The surface is read-only by design: writes come from the faithful pichay
    capture, never from the instance. Exactly search + recall, no store/ingest —
    so an instance can reach its memory but cannot rewrite its own record."""
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert names == {"search", "recall"}


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
