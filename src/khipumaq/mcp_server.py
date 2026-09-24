"""Read-only MCP server over the episodic store.

Two principles, kept from the retired contract layer (spec D2):

1. No write tool. Episodes are written by ingestion of the faithful record —
   Claude Code transcripts and Codex rollouts — never by the instance
   reaching for the store, so the record stays an artifact rather than
   something the instance can edit about itself.
2. Content-free operational events. `search` and `recall` emit identifiers,
   digests, and counts only; never query text, snippets, or episode bodies.

The server says one sentence about itself at start (spec D5), rebuilt from
the store: what it holds, how fresh it is, and when to reach for it.

Run for dogfooding:  uv run python -m khipumaq.mcp_server   (stdio transport)
"""

from mcp.server.fastmcp import FastMCP

from khipumaq.db import get_database, person
from khipumaq.describe import SERVER_NAME
from khipumaq.describe import describe as _describe
from khipumaq.describe import instructions as _instructions
from khipumaq.observability import emit_recall_event, emit_search_event
from khipumaq.recall import recall as _recall
from khipumaq.search import search as _search


def _self_description() -> str:
    """The one sentence the server says about itself (spec D5), rebuilt from
    the store at every start. If the store cannot be reached, say that rather
    than say nothing: the tools below will fail the same way."""
    try:
        return _instructions(get_database(), person=person())
    except Exception as exc:  # noqa: BLE001 — any failure is worth naming
        return (
            f"{SERVER_NAME} could not reach its episode store at start "
            f"({type(exc).__name__}: {exc}); search() and recall() will fail "
            "until that is fixed."
        )


mcp = FastMCP(SERVER_NAME, instructions=_self_description())


@mcp.tool()
def search(
    query: str,
    scope: str = "all",
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """Search every prior session's turns: the user's own words and the
    assistant's full responses, from Claude Code and Codex sessions on every
    machine where khipumaq is installed, keyed by project label. Use it before
    asking what happened, what was decided, or what a prior instance answered.
    `scope` restricts to one project label (see `describe`); "all" searches
    everything. `since`/`until` are ISO dates bounding the episode timestamp;
    a week or a month usually cuts the candidates by an order of magnitude.
    Returns {"total": N, "hits": [...]}: `total` is how many episodes matched
    before `limit`, so if it is in the thousands, narrow with `scope` or
    `since` rather than trust the top ten. Hits carry `key`, `score`, `ts`,
    `experiment_label`, `source_file`, and a 200-char snippet; pass `key` to
    `recall` for the whole episode."""
    result = _search(get_database(), query, scope=scope, limit=limit, since=since, until=until)
    emit_search_event(
        query=query,
        scope=scope,
        since=since,
        until=until,
        total=result["total"],
        returned=len(result["hits"]),
        keys=[hit["key"] for hit in result["hits"]],
    )
    return result


@mcp.tool()
def recall(key: str) -> dict | None:
    """Read one episode in full by the `key` from a search hit: the user's
    message, the assistant's whole response, timestamp, model, project label,
    host, and source file. Null if no episode has that key."""
    episode = _recall(get_database(), key)
    emit_recall_event(key=key, found=episode is not None)
    return episode


@mcp.tool()
def describe() -> dict:
    """What the store holds: episode count, counts by project label and by
    host, and the oldest/newest timestamps. Read-only; use it to pick a
    `scope` for `search` or to check whether ingestion is current."""
    return _describe(get_database())


if __name__ == "__main__":
    mcp.run()
