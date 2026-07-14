from __future__ import annotations

import os
from pathlib import Path

from llm_memory.arango_provider import ArangoProvider
from llm_memory.db import get_database
from llm_memory.provider import EpisodicProvider
from llm_memory.sqlite_provider import SQLiteProvider


def load_provider() -> EpisodicProvider:
    provider_name = os.environ.get("LLM_MEMORY_PROVIDER", "arango")
    if provider_name == "arango":
        return ArangoProvider(get_database())
    if provider_name == "sqlite":
        raw_path = os.environ.get("LLM_MEMORY_SQLITE_PATH")
        if not raw_path:
            raise ValueError("LLM_MEMORY_SQLITE_PATH is required for sqlite")
        return SQLiteProvider(Path(raw_path))
    raise ValueError("LLM_MEMORY_PROVIDER must be 'arango' or 'sqlite'")
