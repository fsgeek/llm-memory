import asyncio
import importlib
import json
from uuid import uuid4

import pytest
import yaml

from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    SOURCE_STATES,
    SUPERSESSIONS,
    ensure_contract_index,
)
from llm_memory.db import get_database
from llm_memory.index import EPISODES, ensure_index
from llm_memory.ingest import ingest_file
from llm_memory import mcp_server


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

    opened = mcp_server.open_episode(episode_ref, [corpus_id])

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
    monkeypatch.setenv("LLM_MEMORY_SOURCES_CONFIG", str(missing_path))

    reloaded = importlib.reload(mcp_server)
    names = {tool.name for tool in asyncio.run(reloaded.mcp.list_tools())}

    assert {"search", "recall"}.issubset(names)
    with pytest.raises(FileNotFoundError, match="missing-sources.yaml"):
        reloaded.search_history("query", ["configured-corpus"])
