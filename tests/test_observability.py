import io
import json
import stat

import pytest

from llm_memory.observability import emit_event, event_log_path


def test_default_and_environment_event_paths(tmp_path):
    assert event_log_path({}, home=tmp_path) == tmp_path / ".local/state/llm-memory/events.jsonl"
    assert event_log_path(
        {"LLM_MEMORY_EVENT_LOG": "/tmp/custom-events.jsonl"}, home=tmp_path
    ).as_posix() == "/tmp/custom-events.jsonl"


def test_emits_one_private_identifier_only_json_line(tmp_path):
    path = tmp_path / "state" / "events.jsonl"

    assert emit_event(
        "open.completed",
        {
            "corpus_ids": ["codex-history"],
            "source_id": "e8c598ae-711b-42b5-b963-eb35fc946d2b",
            "episode_ref": "episode://codex-history/session/episode",
            "standing": "available",
        },
        path=path,
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["event"] == "open.completed"
    assert record["standing"] == "available"
    assert "ts" in record
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "forbidden", ["query", "snippet", "body", "response", "exception_message", "password"]
)
def test_rejects_content_or_secret_fields(tmp_path, forbidden):
    with pytest.raises(ValueError, match="event field"):
        emit_event("search.completed", {forbidden: "do not persist"}, path=tmp_path / "events.jsonl")


def test_rejects_nested_values_even_for_allowed_fields(tmp_path):
    with pytest.raises(ValueError, match="event field"):
        emit_event(
            "server.started",
            {"outcome": {"body": "do not persist"}},
            path=tmp_path / "events.jsonl",
        )


def test_write_failure_reports_stderr_without_raising(tmp_path):
    stderr = io.StringIO()

    assert emit_event("server.started", {}, path=tmp_path, stderr=stderr) is False
    assert "operational event write failed" in stderr.getvalue()


def test_nonserializable_allowed_value_does_not_break_the_operation(tmp_path):
    stderr = io.StringIO()

    assert emit_event(
        "server.started",
        {"outcome": object()},
        path=tmp_path / "events.jsonl",
        stderr=stderr,
    ) is False
    assert "TypeError" in stderr.getvalue()
