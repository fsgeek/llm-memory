import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from khipumaq import mcp_server
from khipumaq.db import get_database
from khipumaq.describe import describe, instructions, project_label, sentence
from khipumaq.index import EPISODES, ensure_index


def _stats(*, episodes=12_345, newest="2026-09-03T12:00:00Z", hosts=None):
    return {
        "episodes": episodes,
        "newest": newest,
        "oldest": "2026-08-01T12:00:00Z",
        "labels": [
            {"label": "qhaway", "count": 1_234},
            {"label": "other", "count": episodes - 1_234},
        ],
        "hosts": hosts
        if hosts is not None
        else [{"host": "workstation", "count": episodes}],
    }


def _parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_describe_reports_inserted_episodes_by_label_and_host():
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    marker = uuid4().hex
    key_prefix = f"describe-test-{marker}"
    label = f"describe-label-{marker}"
    host = f"describe-host-{marker}"
    keys = [f"{key_prefix}-{index}" for index in range(3)]
    before = describe(db)

    try:
        for index, key in enumerate(keys):
            collection.insert(
                {
                    "_key": key,
                    "ts": f"2098-01-01T00:0{index}:00Z",
                    "experiment_label": label,
                    "host": host,
                }
            )

        result = describe(db)
        label_count = next(
            item["count"] for item in result["labels"] if item["label"] == label
        )
        host_count = next(
            item["count"] for item in result["hosts"] if item["host"] == host
        )

        # This suite shares the live store with session hooks, so unrelated
        # episodes can arrive between the two snapshots. The unique label and
        # host assertions below isolate the documents this test inserted.
        assert result["episodes"] >= before["episodes"] + len(keys)
        assert label_count == len(keys)
        assert host_count == len(keys)
        assert isinstance(result["newest"], str)
        assert isinstance(result["oldest"], str)
        assert _parse_iso(result["newest"]) >= _parse_iso(result["oldest"])
    finally:
        for key in keys:
            if collection.has(key):
                collection.delete(key)


def test_sentence_formats_counts_and_optional_project_clause():
    stats = _stats()
    now = datetime(2026, 9, 3, 12, 30, tzinfo=UTC)

    with_project = sentence(stats, "qhaway", now)
    without_project = sentence(stats, None, now)
    missing_project = sentence(stats, "not-ingested", now)

    assert "holds 12,345 episodes" in with_project
    assert "this project is `qhaway` (1,234 episodes)" in with_project
    assert "this project is" not in without_project
    assert "this project is `not-ingested` (0 episodes)" in missing_project


@pytest.mark.parametrize(
    ("newest", "expected_age"),
    [
        ("2026-09-03T11:30:00Z", "30 minutes ago"),
        ("2026-09-03T11:00:00Z", "1 hour ago"),
        ("2026-08-31T12:00:00Z", "3 days ago"),
    ],
)
def test_sentence_describes_newest_episode_age(newest, expected_age):
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

    assert f"newest {expected_age}" in sentence(_stats(newest=newest), now=now)


def test_sentence_warns_only_when_newest_episode_is_older_than_48_hours():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    stale = sentence(_stats(newest="2026-09-01T11:59:00Z"), now=now)
    fresh = sentence(_stats(newest="2026-09-01T13:00:00Z"), now=now)

    assert "ingestion may be broken" in stale
    assert "ingestion may be broken" not in fresh


def test_sentence_reports_that_ingestion_has_not_run_for_empty_store():
    stats = {
        "episodes": 0,
        "newest": None,
        "oldest": None,
        "labels": [],
        "hosts": [],
    }

    assert "ingestion has not run" in sentence(stats)


@pytest.mark.parametrize("label", [None, "qhaway"])
def test_sentence_always_tells_nonempty_store_users_to_search_first(label):
    result = sentence(
        _stats(), label=label, now=datetime(2026, 9, 3, 12, 30, tzinfo=UTC)
    )

    assert "search() first" in result
    assert 'when the user says "I don\'t recall", search()' in result


def test_sentence_uses_configured_person_everywhere_the_user_is_named():
    result = sentence(
        _stats(),
        now=datetime(2026, 9, 3, 12, 30, tzinfo=UTC),
        person="Tony",
    )

    assert "It holds Tony's words" in result
    assert "Before asking Tony what happened or what was decided" in result
    assert 'when Tony says "I don\'t recall", search()' in result
    assert "the user" not in result


def test_sentence_does_not_count_null_hosts_as_machines():
    stats = _stats(
        hosts=[
            {"host": None, "count": 10_000},
            {"host": "workstation", "count": 2_345},
        ]
    )

    result = sentence(stats, now=datetime(2026, 9, 3, 12, 30, tzinfo=UTC))

    assert "on 1 machines" in result
    assert "on 2 machines" not in result


def test_instructions_reflects_episodes_inserted_during_the_test():
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    marker = uuid4().hex
    key_prefix = f"instructions-test-{marker}"
    label = f"instructions-label-{marker}"
    keys = [f"{key_prefix}-{index}" for index in range(2)]
    baseline = describe(db)
    newest_before = (
        _parse_iso(baseline["newest"])
        if baseline["newest"]
        else datetime.now(UTC)
    )
    inserted_oldest = max(datetime.now(UTC), newest_before) + timedelta(days=1)
    inserted_newest = inserted_oldest + timedelta(minutes=1)

    try:
        for key, timestamp in zip(keys, (inserted_oldest, inserted_newest)):
            collection.insert(
                {
                    "_key": key,
                    "ts": timestamp.isoformat().replace("+00:00", "Z"),
                    "experiment_label": label,
                    "host": f"instructions-host-{marker}",
                }
            )

        result = instructions(
            db,
            project_dir=f"/home/x/projects/{label}",
            now=inserted_newest + timedelta(minutes=1),
        )
        live_stats = describe(db)
        live_newest = _parse_iso(live_stats["newest"])
        stale = sentence(live_stats, now=live_newest + timedelta(days=9))
        fresh = sentence(live_stats, now=live_newest + timedelta(minutes=1))

        assert f"this project is `{label}` ({len(keys)} episodes)" in result
        assert "ingestion may be broken" in stale
        assert "ingestion may be broken" not in fresh
    finally:
        for key in keys:
            if collection.has(key):
                collection.delete(key)


def test_instructions_passes_person_to_sentence():
    class FakeAql:
        def execute(self, _query, bind_vars):
            return iter([_stats()])

    class FakeDb:
        aql = FakeAql()

    result = instructions(
        FakeDb(),
        now=datetime(2026, 9, 3, 12, 30, tzinfo=UTC),
        person="Ada",
    )

    assert "It holds Ada's words" in result
    assert "the user's words" not in result


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/home/tony/projects/wamason.com", "wamason-com"),
        ("/home/tony/projects/qhaway", "qhaway"),
    ],
)
def test_project_label_from_explicit_project_dir(path, expected):
    assert project_label(path) == expected


def test_project_label_is_none_without_argument_or_environment(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    assert project_label() is None


def test_project_label_uses_claude_project_dir_environment(monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/home/tony/projects/wamason.com")

    assert project_label() == "wamason-com"


def test_mcp_instructions_promote_search():
    exposed = getattr(mcp_server.mcp, "instructions", None)
    if exposed is None:
        exposed = mcp_server.mcp._mcp_server.instructions

    assert isinstance(exposed, str)
    assert exposed
    assert "search() first" in exposed


def test_mcp_exposes_describe_tool_and_returns_the_census():
    names = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}
    result = mcp_server.describe()

    assert "describe" in names
    assert isinstance(result, dict)
    assert set(result) == {
        "episodes",
        "newest",
        "oldest",
        "labels",
        "hosts",
    }


def test_self_description_names_an_unreachable_store(monkeypatch):
    def unreachable():
        raise ConnectionError("nope")

    monkeypatch.setattr(mcp_server, "get_database", unreachable)

    result = mcp_server._self_description()

    assert "could not reach" in result
    assert "ConnectionError" in result
