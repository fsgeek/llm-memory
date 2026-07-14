from __future__ import annotations

from llm_memory.arango_provider import ARANGO_DESCRIPTOR, ArangoProvider
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    SOURCE_STATES,
    SUPERSESSIONS,
)
from llm_memory.db import get_database
from llm_memory.sqlite_history import SQLITE_STRATEGY

from provider_contract import assert_portable_provider_contract


def test_arango_provider_satisfies_portable_contract(synthetic_source):
    db = get_database()
    try:
        assert_portable_provider_contract(
            ArangoProvider(db),
            synthetic_source,
            strategy=ARANGO_DESCRIPTOR.strategies[0],
            foreign_strategy=SQLITE_STRATEGY,
        )
    finally:
        for collection_name in (
            CONTRACT_EPISODES,
            SOURCE_STATES,
            SUPERSESSIONS,
        ):
            if db.has_collection(collection_name):
                db.aql.execute(
                    """
                    FOR doc IN @@collection
                        FILTER STARTS_WITH(doc.corpus_id, @prefix)
                        REMOVE doc IN @@collection
                    """,
                    bind_vars={
                        "@collection": collection_name,
                        "prefix": synthetic_source.corpus_id.rsplit("-corpus", 1)[0],
                    },
                )
