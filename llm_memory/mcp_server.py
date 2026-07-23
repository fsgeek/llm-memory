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
from llm_memory.enrollment import EnrollmentRegistry, load_registry
from llm_memory.observability import (
    emit_failure_event,
    emit_open_event,
    emit_reconciliation_event,
    emit_search_event,
    emit_server_event,
)
from llm_memory.opening import open_episode as _open_episode
from llm_memory.provider import EpisodicProvider
from llm_memory.provider_config import load_provider
from llm_memory.recall import recall as _recall
from llm_memory.reconcile import WorkBudget
from llm_memory.search import search as _search

# A contract search may reconcile at most one megabyte of source data per call.
_DEFAULT_RECONCILIATION_MAX_BYTES = 1_000_000
_lifespan_active = False
_selected_provider: EpisodicProvider | None = None
_selected_registry: EnrollmentRegistry | None = None


def _budget() -> WorkBudget:
    return WorkBudget(
        _DEFAULT_RECONCILIATION_MAX_BYTES,
        datetime.now(UTC),
    )


def _contract_runtime() -> tuple[EpisodicProvider, EnrollmentRegistry]:
    if _selected_provider is None or _selected_registry is None:
        raise RuntimeError("episodic provider lifespan is not active")
    return _selected_provider, _selected_registry


def _sole_strategy(capabilities: object) -> str:
    if not isinstance(capabilities, dict):
        raise RuntimeError(
            "selected provider must declare exactly one nonempty string strategy"
        )
    strategies = capabilities.get("strategies")
    if (
        not isinstance(strategies, list)
        or len(strategies) != 1
        or not isinstance(strategies[0], str)
        or not strategies[0].strip()
    ):
        raise RuntimeError(
            "selected provider must declare exactly one nonempty string strategy"
        )
    return strategies[0]


def _emit_reconciliation(report) -> None:
    for corpus in report.corpus_standing:
        for source in corpus["sources"]:
            for member in source["members"]:
                emit_reconciliation_event(
                    corpus_id=corpus["corpus_id"],
                    source_id=source["source_id"],
                    member_id=member["member_id"],
                    source_standing=member["source_standing"],
                    index_standing=member["index_standing"],
                    episode_count=member["episode_count"],
                    bytes_read=report.bytes_read,
                    duration_ms=report.elapsed_ms,
                    work_exhausted=report.work_exhausted,
                )


@asynccontextmanager
async def _lifespan(_server) -> AsyncIterator[dict]:
    global _lifespan_active, _selected_provider, _selected_registry

    if _lifespan_active:
        raise RuntimeError("episodic provider lifespan is already active")
    _lifespan_active = True
    emit_server_event("starting")
    try:
        try:
            provider = load_provider()
            provider.ensure()
            try:
                registry = load_registry()
            except FileNotFoundError:
                registry = None
            if registry is not None:
                report = provider.reconcile(registry, _budget())
                _emit_reconciliation(report)
                _selected_provider, _selected_registry = provider, registry
        except BaseException as exc:
            emit_failure_event("server", exc)
            raise
        if registry is None:
            emit_server_event("started", outcome="enrollment_missing")
            yield {}
        else:
            emit_server_event("started")
            yield {"startup_reconciliation": report}
    finally:
        _lifespan_active = False
        _selected_provider = _selected_registry = None
        emit_server_event("stopped")


mcp = FastMCP("llm-memory", lifespan=_lifespan)


@mcp.tool()
def search(query: str, scope: str = "all", limit: int = 10) -> list[dict]:
    """Search episodic memory for prior turns. Matches the conversational
    response, the user's message, and state text. `scope` is the experiment
    label (e.g. "claude_code" for live sessions, "all" for everything). Returns
    ranked hits, each with `key`, `cycle`, `score`, and a snippet. Pass a hit's
    `key` to `recall` to read that episode in full."""
    return _search(get_database(), query, scope=scope, limit=limit)


@mcp.tool()
def recall(key: str) -> dict | None:
    """Fetch one episode IN FULL by the `key` from a search hit. Returns the whole
    episode (full response and user message, not the truncated snippet), or null
    if no episode has that key."""
    return _recall(get_database(), key)


@mcp.tool()
def search_history(query: str, corpus_ids: list[str], limit: int = 10) -> dict:
    """Search enrolled episodic sources through the versioned contract.

    Reconciliation reads at most 1,000,000 source bytes per invocation.
    """
    try:
        provider, registry = _contract_runtime()
        strategy = _sole_strategy(provider.capabilities())
        request = SearchRequest.create(
            query,
            corpus_ids,
            limit=limit,
            strategy=strategy,
        )
        response = provider.search(registry, request, _budget())
    except BaseException as exc:
        emit_failure_event("search", exc, corpus_ids=corpus_ids)
        raise
    emit_search_event(
        corpus_ids=corpus_ids,
        returned_count=response.get("returned_count", 0),
        episode_refs=[
            result["episode_ref"] for result in response.get("results", ())
        ],
    )
    return response


@mcp.tool()
def open_episode(episode_ref: str, active_corpus_ids: list[str]) -> dict:
    """Open one contract episode from its enrolled authoritative source."""
    try:
        provider, registry = _contract_runtime()
        response = _open_episode(
            registry,
            episode_ref,
            active_corpus_ids,
            provider.resolve_supersession,
        )
    except BaseException as exc:
        emit_failure_event(
            "open",
            exc,
            corpus_ids=active_corpus_ids,
            episode_ref=episode_ref,
        )
        raise
    emit_open_event(
        corpus_ids=active_corpus_ids,
        episode_ref=episode_ref,
        standing=response.get("standing", "unknown"),
    )
    return response


if __name__ == "__main__":
    mcp.run()
