import sys

import pytest


class DiscardingQueryHistory:
    def search(self, **kwargs):
        pass

    def recall(self, **kwargs):
        pass

    def describe(self):
        pass


@pytest.fixture(autouse=True)
def isolate_mcp_query_history(monkeypatch):
    """Server-tool tests must never write to the shared queries collection."""
    mcp_server = sys.modules.get("khipumaq.mcp_server")
    if mcp_server is None:
        return

    monkeypatch.setattr(mcp_server, "history", DiscardingQueryHistory())
