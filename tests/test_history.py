from uuid import UUID

import pytest

from khipumaq import history as history_module
from khipumaq import mcp_server
from khipumaq.history import MAX_HITS, QueryHistory


class FakeCollection:
    def __init__(self, *, insert_error=None):
        self.documents = []
        self.insert_error = insert_error
        self.insert_attempts = 0

    def insert(self, document):
        self.insert_attempts += 1
        if self.insert_error is not None:
            raise self.insert_error
        key = f"query-{len(self.documents) + 1}"
        self.documents.append({"_key": key, **document})
        return {"_key": key}


class FakeDatabase:
    def __init__(self, collection=None):
        self.collections = {}
        if collection is not None:
            self.collections["queries"] = collection
        self.created = []

    def has_collection(self, name):
        return name in self.collections

    def create_collection(self, name):
        self.created.append(name)
        self.collections[name] = FakeCollection()
        return self.collections[name]

    def collection(self, name):
        return self.collections[name]


@pytest.fixture
def recorder(monkeypatch):
    monkeypatch.setattr(
        history_module.uuid,
        "uuid4",
        lambda: UUID("10000000-0000-0000-0000-000000000023"),
    )
    monkeypatch.setattr(history_module.socket, "gethostname", lambda: "test-host")
    monkeypatch.setattr(history_module, "_now", lambda: "2026-09-24T18:00:00Z")
    database = FakeDatabase()
    return QueryHistory(lambda: database, project="test-project"), database


def test_search_records_query_window_counts_ranked_hits_and_context(recorder):
    history, database = recorder
    result = {
        "total": 17,
        "hits": [
            {
                "key": "episode-b",
                "score": 9.5,
                "snippet": "must not be recorded",
                "response": "must not be recorded either",
            },
            {
                "key": "episode-a",
                "score": 4.25,
                "user_message": "nor this",
            },
        ],
    }

    history.search(
        query="why did the index change?",
        scope="khipumaq",
        since="2026-09-01T00:00:00Z",
        until="2026-09-24T23:59:59Z",
        limit=2,
        result=result,
        elapsed=0.01234,
    )

    assert database.collection("queries").documents == [
        {
            "_key": "query-1",
            "ts": "2026-09-24T18:00:00Z",
            "session": "10000000-0000-0000-0000-000000000023",
            "project": "test-project",
            "host": "test-host",
            "kind": "search",
            "query": "why did the index change?",
            "scope": "khipumaq",
            "since": "2026-09-01T00:00:00Z",
            "until": "2026-09-24T23:59:59Z",
            "limit": 2,
            "total": 17,
            "returned": 2,
            "hits": [
                {"key": "episode-b", "rank": 0, "score": 9.5},
                {"key": "episode-a", "rank": 1, "score": 4.25},
            ],
            "elapsed_ms": 12.3,
        }
    ]


def test_search_caps_recorded_hits_but_preserves_returned_count(recorder):
    history, database = recorder
    result = {
        "total": 1000,
        "hits": [
            {"key": f"episode-{rank}", "score": 1000 - rank}
            for rank in range(MAX_HITS + 7)
        ],
    }

    history.search(
        query="wide query",
        scope="all",
        since=None,
        until=None,
        limit=MAX_HITS + 7,
        result=result,
        elapsed=0.001,
    )

    document = database.collection("queries").documents[0]
    assert document["returned"] == 107
    assert len(document["hits"]) == 100
    assert document["hits"][0] == {
        "key": "episode-0",
        "rank": 0,
        "score": 1000,
    }
    assert document["hits"][-1] == {
        "key": "episode-99",
        "rank": 99,
        "score": 901,
    }
    assert all(hit["key"] != "episode-100" for hit in document["hits"])


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A23 bug: QueryHistory remembers only the capped recorded hits, so a "
        "returned key beyond MAX_HITS cannot be linked by recall"
    ),
)
def test_recall_links_to_returned_key_beyond_recording_cap(recorder):
    history, database = recorder
    history.search(
        query="wide query",
        scope="all",
        since=None,
        until=None,
        limit=MAX_HITS + 1,
        result={
            "total": MAX_HITS + 1,
            "hits": [
                {"key": f"episode-{rank}", "score": MAX_HITS - rank}
                for rank in range(MAX_HITS + 1)
            ],
        },
        elapsed=0.01,
    )

    history.recall(key=f"episode-{MAX_HITS}", found=True)

    recall = database.collection("queries").documents[-1]
    assert recall["after_search"] == "query-1"
    assert recall["rank"] == MAX_HITS


def test_recall_links_to_the_search_that_returned_the_key(recorder):
    history, database = recorder
    history.search(
        query="first query",
        scope="all",
        since=None,
        until=None,
        limit=2,
        result={
            "total": 2,
            "hits": [
                {"key": "other", "score": 8.0},
                {"key": "chosen", "score": 7.0},
            ],
        },
        elapsed=0.01,
    )

    history.recall(key="chosen", found=True)

    assert database.collection("queries").documents[-1] == {
        "_key": "query-2",
        "ts": "2026-09-24T18:00:00Z",
        "session": "10000000-0000-0000-0000-000000000023",
        "project": "test-project",
        "host": "test-host",
        "kind": "recall",
        "key": "chosen",
        "found": True,
        "after_search": "query-1",
        "rank": 1,
    }


def test_recall_links_to_latest_search_that_returned_the_key(recorder):
    history, database = recorder
    history.search(
        query="old query",
        scope="all",
        since=None,
        until=None,
        limit=1,
        result={"total": 1, "hits": [{"key": "chosen", "score": 3.0}]},
        elapsed=0.01,
    )
    history.search(
        query="new query",
        scope="all",
        since=None,
        until=None,
        limit=3,
        result={
            "total": 3,
            "hits": [
                {"key": "first", "score": 5.0},
                {"key": "second", "score": 4.0},
                {"key": "chosen", "score": 3.0},
            ],
        },
        elapsed=0.01,
    )
    history.search(
        query="newer unrelated query",
        scope="all",
        since=None,
        until=None,
        limit=1,
        result={"total": 1, "hits": [{"key": "unrelated", "score": 6.0}]},
        elapsed=0.01,
    )

    history.recall(key="chosen", found=True)

    recall = database.collection("queries").documents[-1]
    assert recall["after_search"] == "query-2"
    assert recall["rank"] == 2
    assert recall["found"] is True


def test_recall_of_never_returned_key_records_no_search_link(recorder):
    history, database = recorder

    history.recall(key="missing", found=False)

    recall = database.collection("queries").documents[0]
    assert recall["after_search"] is None
    assert recall["rank"] is None
    assert recall["found"] is False


def test_describe_records_its_kind_and_context(recorder):
    history, database = recorder

    history.describe()

    assert database.collection("queries").documents == [
        {
            "_key": "query-1",
            "ts": "2026-09-24T18:00:00Z",
            "session": "10000000-0000-0000-0000-000000000023",
            "project": "test-project",
            "host": "test-host",
            "kind": "describe",
        }
    ]


def test_first_write_creates_queries_collection_when_absent(recorder):
    history, database = recorder
    assert not database.has_collection("queries")

    history.describe()

    assert database.created == ["queries"]
    assert len(database.collection("queries").documents) == 1


def test_recording_methods_never_raise_when_database_getter_raises():
    calls = []

    def unavailable_database():
        calls.append("get_database")
        raise ConnectionError("unavailable")

    history = QueryHistory(unavailable_database)

    history.search(
        query="query",
        scope="all",
        since=None,
        until=None,
        limit=1,
        result={"total": 1, "hits": [{"key": "episode", "score": 1.0}]},
        elapsed=0.01,
    )
    history.recall(key="episode", found=True)
    history.describe()

    assert calls == ["get_database", "get_database", "get_database"]


def test_recording_methods_never_raise_when_collection_insert_raises():
    collection = FakeCollection(insert_error=RuntimeError("insert failed"))
    database = FakeDatabase(collection)
    history = QueryHistory(lambda: database)

    history.search(
        query="query",
        scope="all",
        since=None,
        until=None,
        limit=1,
        result={"total": 1, "hits": [{"key": "episode", "score": 1.0}]},
        elapsed=0.01,
    )
    history.recall(key="episode", found=True)
    history.describe()

    assert collection.insert_attempts == 3


@pytest.mark.parametrize(
    ("result", "recorded_hits"),
    [
        pytest.param(None, None, id="none"),
        pytest.param({"total": 0}, None, id="missing-hits"),
        pytest.param(
            {"total": 1, "hits": [{"key": "episode"}]},
            [{"key": "episode", "rank": 0, "score": None}],
            id="hit-missing-score",
        ),
    ],
)
def test_search_recording_never_raises_for_malformed_results(
    recorder, result, recorded_hits
):
    history, database = recorder

    history.search(
        query="query",
        scope="all",
        since=None,
        until=None,
        limit=1,
        result=result,
        elapsed=0.01,
    )

    documents = database.collections.get("queries", FakeCollection()).documents
    if recorded_hits is None:
        assert documents == []
    else:
        assert documents[0]["hits"] == recorded_hits


def test_each_mcp_tool_returns_normally_when_history_database_getter_raises(
    monkeypatch,
):
    history_calls = []
    database = object()
    search_result = {
        "total": 1,
        "hits": [{"key": "episode", "score": 1.0}],
    }
    episode = {"_key": "episode", "response": "full response"}
    description = {
        "episodes": 1,
        "newest": "2026-09-24T00:00:00Z",
        "oldest": "2026-09-24T00:00:00Z",
        "labels": {"test": 1},
        "hosts": {"test-host": 1},
    }

    def unavailable_database():
        history_calls.append("get_database")
        raise ConnectionError("history unavailable")

    monkeypatch.setattr(mcp_server, "history", QueryHistory(unavailable_database))
    monkeypatch.setattr(mcp_server, "get_database", lambda: database)
    monkeypatch.setattr(mcp_server, "_search", lambda *args, **kwargs: search_result)
    monkeypatch.setattr(mcp_server, "_recall", lambda *args, **kwargs: episode)
    monkeypatch.setattr(mcp_server, "_describe", lambda *args, **kwargs: description)
    monkeypatch.setattr(mcp_server, "emit_search_event", lambda **kwargs: None)
    monkeypatch.setattr(mcp_server, "emit_recall_event", lambda **kwargs: None)

    assert mcp_server.search("query") is search_result
    assert mcp_server.recall("episode") is episode
    assert mcp_server.describe() is description
    assert history_calls == ["get_database", "get_database", "get_database"]


def test_each_mcp_tool_returns_normally_when_history_collection_insert_raises(
    monkeypatch,
):
    database = object()
    search_result = {"total": 1, "hits": [{"key": "episode", "score": 1.0}]}
    episode = {"_key": "episode", "response": "full response"}
    description = {
        "episodes": 1,
        "newest": "2026-09-24T00:00:00Z",
        "oldest": "2026-09-24T00:00:00Z",
        "labels": {"test": 1},
        "hosts": {"test-host": 1},
    }
    collection = FakeCollection(insert_error=RuntimeError("insert failed"))
    history_database = FakeDatabase(collection)
    monkeypatch.setattr(mcp_server, "history", QueryHistory(lambda: history_database))
    monkeypatch.setattr(mcp_server, "get_database", lambda: database)
    monkeypatch.setattr(
        mcp_server, "_search", lambda *args, **kwargs: search_result
    )
    monkeypatch.setattr(mcp_server, "_recall", lambda *args, **kwargs: episode)
    monkeypatch.setattr(mcp_server, "_describe", lambda *args, **kwargs: description)
    monkeypatch.setattr(mcp_server, "emit_search_event", lambda **kwargs: None)
    monkeypatch.setattr(mcp_server, "emit_recall_event", lambda **kwargs: None)

    assert mcp_server.search("query") is search_result
    assert mcp_server.recall("episode") is episode
    assert mcp_server.describe() is description
    assert collection.insert_attempts == 3
