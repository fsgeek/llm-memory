from __future__ import annotations

import configparser
from uuid import uuid4

import pytest
from arango import ArangoClient
from arango.exceptions import ArangoError

from llm_memory.arango_provider import ARANGO_DESCRIPTOR, ArangoProvider
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    CONTRACT_VIEW,
    SOURCE_STATES,
    SUPERSESSIONS,
)
from llm_memory.db import get_database
from llm_memory.provider import PurgeScope
from llm_memory.sqlite_history import SQLITE_STRATEGY

from provider_contract import assert_portable_provider_contract


@pytest.fixture
def disposable_arango_database():
    config = configparser.ConfigParser()
    config.read("config/db-config.ini")
    settings = config["database"]
    client = ArangoClient(
        hosts=f"http://{settings['host']}:{settings['port']}"
    )
    system = client.db(
        "_system",
        username=settings["user_name"],
        password=settings["user_password"],
    )
    database_name = f"llm_memory_portable_{uuid4().hex}"
    created = False
    try:
        try:
            system.databases()
            created = bool(
                system.create_database(
                    database_name,
                    users=[
                        {
                            "username": settings["user_name"],
                            "password": settings["user_password"],
                            "active": True,
                            "extra": {},
                        }
                    ],
                )
            )
        except ArangoError as exc:
            pytest.skip(
                "configured Arango credentials cannot provision an owned "
                f"disposable database: {exc}"
            )
        if not created:
            pytest.skip("Arango did not create the disposable test database")
        yield client.db(
            database_name,
            username=settings["user_name"],
            password=settings["user_password"],
        )
    finally:
        if created:
            assert system.delete_database(database_name, ignore_missing=True)


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


def test_arango_full_removal_uses_owned_disposable_database(
    disposable_arango_database, synthetic_source
):
    db = disposable_arango_database
    provider = ArangoProvider(db)
    rewritten_bytes = assert_portable_provider_contract(
        provider,
        synthetic_source,
        strategy=ARANGO_DESCRIPTOR.strategies[0],
        foreign_strategy=SQLITE_STRATEGY,
    )
    assert all(
        db.has_collection(name)
        for name in (CONTRACT_EPISODES, SOURCE_STATES, SUPERSESSIONS)
    )
    assert CONTRACT_VIEW in {view["name"] for view in db.views()}

    removal = provider.remove_all()

    assert removal == {
        "removed_objects": [
            CONTRACT_VIEW,
            CONTRACT_EPISODES,
            SOURCE_STATES,
            SUPERSESSIONS,
        ],
        "declared_losses": ["retained supersession observations"],
    }
    assert not any(
        db.has_collection(name)
        for name in (CONTRACT_EPISODES, SOURCE_STATES, SUPERSESSIONS)
    )
    assert CONTRACT_VIEW not in {view["name"] for view in db.views()}
    unavailable = provider.measure(PurgeScope())
    assert unavailable.standing == "unavailable"
    assert unavailable.observations == {
        "episode_documents": None,
        "source_state_documents": None,
        "supersession_documents": None,
    }
    assert synthetic_source.path.read_bytes() == rewritten_bytes
