"""Read-only MCP server over episodic memory.

Two tools, both reads: `search` ranks episodes (conversation-inclusive BM25) and
`recall` fetches one episode in full by the key a search hit carries. There is no
write tool on purpose — episodes are written by the faithful pichay capture, not
by the instance reaching for them, so the record stays an artifact rather than
something the instance can edit about itself.

Run for dogfooding:  uv run python -m llm_memory.mcp_server   (stdio transport)
"""

from mcp.server.fastmcp import FastMCP

from llm_memory.db import get_database
from llm_memory.recall import recall as _recall
from llm_memory.search import search as _search

mcp = FastMCP("llm-memory")
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


if __name__ == "__main__":
    mcp.run()
