"""Read-only MCP server over episodic memory.

`search_history` and `open_episode` expose the versioned episodic contract.
`search` and `recall` remain legacy reduced-standing compatibility tools. There
is no write tool on purpose — episodes are written by the faithful pichay
capture, not by the instance reaching for them, so the record stays an artifact
rather than something the instance can edit about itself.

Run for dogfooding:  uv run python -m llm_memory.mcp_server   (stdio transport)
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import AsyncIterator

from mcp.server.fastmcp import FastMCP

from llm_memory.contract import SearchRequest
from llm_memory.db import get_database
from llm_memory.enrollment import load_registry
from llm_memory.history import open_episode as _open_episode
from llm_memory.history import search_history as _search_history
from llm_memory.recall import recall as _recall
from llm_memory.reconcile import WorkBudget, reconcile_registry
from llm_memory.search import search as _search

# A contract search may reconcile at most one megabyte of source data per call.
_DEFAULT_RECONCILIATION_MAX_BYTES = 1_000_000


@asynccontextmanager
async def _lifespan(_server) -> AsyncIterator[dict]:
    try:
        registry = load_registry()
    except FileNotFoundError:
        yield {}
        return
    report = reconcile_registry(
        _db,
        registry,
        WorkBudget(
            _DEFAULT_RECONCILIATION_MAX_BYTES,
            datetime.now(UTC),
        ),
    )
    yield {"startup_reconciliation": report}


mcp = FastMCP("llm-memory", lifespan=_lifespan)
_db = get_database()


@mcp.tool()
def search(query: str, scope: str = "all", limit: int = 10) -> list[dict]:
    """Search episodic memory for prior turns. Matches the conversational
    response, the user's message, and state text. `scope` is the experiment
    label (e.g. "claude_code" for live sessions, "all" for everything). Returns
    ranked hits, each with `key`, `cycle`, `score`, and a snippet. Pass a hit's
    `key` to `recall` to read that episode in full."""
    return _search(_db, query, scope=scope, limit=limit)


@mcp.tool()
def recall(key: str) -> dict | None:
    """Fetch one episode IN FULL by the `key` from a search hit. Returns the whole
    episode (full response and user message, not the truncated snippet), or null
    if no episode has that key."""
    return _recall(_db, key)


@mcp.tool()
def search_history(query: str, corpus_ids: list[str], limit: int = 10) -> dict:
    """Search enrolled episodic sources through the versioned contract.

    Reconciliation reads at most 1,000,000 source bytes per invocation.
    """
    registry = load_registry()
    request = SearchRequest.create(query, corpus_ids, limit=limit)
    budget = WorkBudget(
        _DEFAULT_RECONCILIATION_MAX_BYTES,
        datetime.now(UTC),
    )
    return _search_history(_db, registry, request, budget)


@mcp.tool()
def open_episode(episode_ref: str, active_corpus_ids: list[str]) -> dict:
    """Open one contract episode from its enrolled authoritative source."""
    registry = load_registry()
    return _open_episode(_db, registry, episode_ref, active_corpus_ids)


if __name__ == "__main__":
    mcp.run()
