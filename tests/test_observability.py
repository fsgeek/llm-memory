import hashlib
import json
import stat
from datetime import datetime

from llm_memory.observability import (
    emit_ingest_event,
    emit_recall_event,
    emit_search_event,
)


def _event_log(tmp_path, monkeypatch, name="events.jsonl"):
    path = tmp_path / name
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(path))
    return path


def _read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_utc_timestamp(record):
    assert record["ts"].endswith("Z")
    assert datetime.fromisoformat(record["ts"].removesuffix("Z") + "+00:00")


def test_emit_search_event_appends_the_documented_content_free_record(
    tmp_path, monkeypatch
):
    path = _event_log(tmp_path, monkeypatch)
    query = "literal private query"

    written = emit_search_event(
        query=query,
        scope="hamutay",
        since="2026-08-01T00:00:00Z",
        until="2026-09-01T00:00:00Z",
        total=13,
        returned=2,
        keys=["episode-a", "episode-b"],
    )

    text = path.read_text(encoding="utf-8")
    records = _read_records(path)
    assert written is True
    assert len(records) == 1
    assert records[0] == {
        "event": "search.completed",
        "keys_sha256": hashlib.sha256(b"episode-a\nepisode-b").hexdigest(),
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "returned": 2,
        "scope": "hamutay",
        "since": "2026-08-01T00:00:00Z",
        "total": 13,
        "ts": records[0]["ts"],
        "until": "2026-09-01T00:00:00Z",
    }
    _assert_utc_timestamp(records[0])
    assert query not in text


def test_emit_recall_event_appends_the_documented_record(tmp_path, monkeypatch):
    path = _event_log(tmp_path, monkeypatch)

    written = emit_recall_event(key="episode-a", found=True)

    records = _read_records(path)
    assert written is True
    assert len(records) == 1
    assert records[0] == {
        "event": "recall.completed",
        "found": True,
        "key": "episode-a",
        "ts": records[0]["ts"],
    }
    _assert_utc_timestamp(records[0])


def test_emit_ingest_event_appends_the_documented_record(tmp_path, monkeypatch):
    path = _event_log(tmp_path, monkeypatch)
    source_file = tmp_path / "sessions" / "session.jsonl"

    written = emit_ingest_event(
        kind="claude-session",
        label="hamutay",
        host="test-host",
        count=4,
        source_file=source_file,
    )

    records = _read_records(path)
    assert written is True
    assert len(records) == 1
    assert records[0] == {
        "count": 4,
        "event": "ingest.completed",
        "host": "test-host",
        "kind": "claude-session",
        "label": "hamutay",
        "source_file": str(source_file),
        "ts": records[0]["ts"],
    }
    _assert_utc_timestamp(records[0])


def test_events_append_as_separate_json_lines(tmp_path, monkeypatch):
    path = _event_log(tmp_path, monkeypatch)

    assert emit_recall_event(key="episode-a", found=True) is True
    assert emit_recall_event(key="episode-b", found=False) is True

    records = _read_records(path)
    assert [record["key"] for record in records] == ["episode-a", "episode-b"]
    assert path.read_bytes().count(b"\n") == 2


def test_event_log_is_created_with_private_permissions_and_parent_directories(
    tmp_path, monkeypatch
):
    path = _event_log(tmp_path, monkeypatch, "new/parent/events.jsonl")

    assert emit_recall_event(key="episode-a", found=True) is True

    assert path.parent.is_dir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_oversized_event_is_dropped(tmp_path, monkeypatch):
    path = _event_log(tmp_path, monkeypatch)

    written = emit_recall_event(key="x" * 8192, found=True)

    assert written is False
    assert not path.exists()


def test_unwritable_event_path_reports_once_and_does_not_raise(
    tmp_path, monkeypatch, capsys
):
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("keep me", encoding="utf-8")
    path = blocking_file / "events.jsonl"
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(path))

    written = emit_recall_event(key="episode-a", found=True)

    captured = capsys.readouterr()
    assert written is False
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert captured.err.startswith("operational event write failed: ")
    assert blocking_file.read_text(encoding="utf-8") == "keep me"
