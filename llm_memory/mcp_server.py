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
from llm_memory.describe import SERVER_NAME
from llm_memory.describe import describe as _describe
from llm_memory.describe import instructions as _instructions
from llm_memory.enrollment import EnrollmentRegistry, load_registry
from llm_memory.observability import (
    emit_failure_event,
    emit_initialization_event,
    emit_open_event,
    emit_reconciliation_event,
    emit_reconciliation_started,
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


def _registry_counts(registry: object) -> tuple[int, int]:
    sources = tuple(getattr(registry, "sources", ()))
    enabled_sources = tuple(
        source for source in sources if getattr(source, "enabled", True)
    )
    return (
        len({source.corpus_id for source in enabled_sources}),
        len(enabled_sources),
    )


@asynccontextmanager
async def _lifespan(_server) -> AsyncIterator[dict]:
    global _lifespan_active, _selected_provider, _selected_registry

    if _lifespan_active:
        raise RuntimeError("episodic provider lifespan is already active")
    _lifespan_active = True
    try:
        emit_server_event("starting")
        try:
            provider = load_provider()
            provider.ensure()
        except BaseException as exc:
            emit_failure_event("provider", exc)
            raise
        emit_initialization_event("provider", outcome="initialized")
        try:
            try:
                registry = load_registry()
            except FileNotFoundError:
                registry = None
        except BaseException as exc:
            emit_failure_event("enrollment", exc)
            raise
        if registry is None:
            emit_initialization_event("enrollment", outcome="missing")
        else:
            emit_initialization_event("enrollment", outcome="initialized")
            corpus_count, source_count = _registry_counts(registry)
            emit_reconciliation_started(
                corpus_count=corpus_count, source_count=source_count
            )
            try:
                report = provider.reconcile(registry, _budget())
            except BaseException as exc:
                emit_failure_event("reconciliation", exc)
                raise
            _emit_reconciliation(report)
            _selected_provider, _selected_registry = provider, registry
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


def _self_description() -> str:
    """The one sentence the server says about itself (spec D5), rebuilt from
    the store at every start. If the store cannot be reached, say that rather
    than say nothing: the tools below will fail the same way."""
    try:
        return _instructions(get_database())
    except Exception as exc:  # noqa: BLE001 — any failure is worth naming
        return (
            f"{SERVER_NAME} could not reach its episode store at start "
            f"({type(exc).__name__}: {exc}); search() and recall() will fail "
            "until that is fixed."
        )


mcp = FastMCP(SERVER_NAME, instructions=_self_description(), lifespan=_lifespan)


@mcp.tool()
def search(query: str, scope: str = "all", limit: int = 10) -> list[dict]:
    """Search every prior session's turns: the user's own words and the
    assistant's full responses, from Claude Code sessions on all of Tony's
    machines (March 2026 onward), keyed by project label. Use it before asking
    what happened, what was decided, or what a prior instance answered.
    `scope` restricts to one project label (see `describe`); "all" searches
    everything. Returns BM25-ranked hits with `key`, `score`, and a 200-char
    snippet; pass `key` to `recall` for the whole episode."""
    return _search(get_database(), query, scope=scope, limit=limit)


@mcp.tool()
def recall(key: str) -> dict | None:
    """Read one episode in full by the `key` from a search hit: the user's
    message, the assistant's whole response, timestamp, model, project label,
    host, and source file. Null if no episode has that key."""
    return _recall(get_database(), key)


@mcp.tool()
def describe() -> dict:
    """What the store holds: episode count, counts by project label and by
    host, and the oldest/newest timestamps. Read-only; use it to pick a
    `scope` for `search` or to check whether ingestion is current."""
    return _describe(get_database())


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
