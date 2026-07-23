import io
import json
import stat

import pytest

from llm_memory.contract import EpisodeReference
from llm_memory.observability import emit_event, event_log_path


def _episode_ref() -> str:
    reference = EpisodeReference.build(
        corpus_id="codex-history",
        source_id="machine-uuid",
        native_session_id="native-session",
        canonicalization_version=1,
        boundary_version=1,
        event_token="0",
        content_digest="0" * 64,
    )
    return str(reference)


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
            "episode_ref": _episode_ref(),
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


@pytest.mark.parametrize(
    ("event", "fields"),
    [
        ("arbitrary.event", {}),
        ("search.completed", {"corpus_ids": ("codex-history",)}),
        ("search.completed", {"corpus_ids": ["secret prose"]}),
        ("search.completed", {"episode_ref": "episode://codex-history/session/episode"}),
        ("search.completed", {"episode_refs": _episode_ref()}),
        ("search.completed", {"source_id": "secret response body"}),
        ("search.completed", {"source_id": "x" * 129}),
        ("search.completed", {"provider": "password=hunter2"}),
        ("search.failed", {"exception_class": "RuntimeError: secret"}),
        ("search.failed", {"diagnostic_code": "secret_error"}),
        ("open.completed", {"outcome": "please persist this response"}),
        ("open.completed", {"standing": "arbitrary standing"}),
        ("reconcile.completed", {"bytes_read": True}),
        ("reconcile.completed", {"episode_count": -1}),
        ("reconcile.completed", {"duration_ms": float("nan")}),
        ("reconcile.completed", {"work_exhausted": 1}),
    ],
)
def test_rejects_values_that_could_smuggle_content_or_break_schema(tmp_path, event, fields):
    with pytest.raises(ValueError, match="event"):
        emit_event(event, fields, path=tmp_path / "events.jsonl")


def test_accepts_only_the_current_operational_value_vocabulary(tmp_path):
    assert emit_event(
        "reconcile.completed",
        {
            "adapter": "codex_jsonl",
            "bytes_read": 1,
            "corpus_ids": ["codex-history"],
            "diagnostic_code": "contract_search_failed",
            "duration_ms": 1.5,
            "episode_count": 2,
            "episode_ref": _episode_ref(),
            "episode_refs": [_episode_ref()],
            "exception_class": "RuntimeError",
            "index_standing": "available",
            "member_id": "member-1",
            "operation_id": "operation-1",
            "outcome": "completed",
            "provider": "arango",
            "returned_count": 3,
            "source_id": "machine-uuid",
            "source_standing": "available",
            "standing": "available",
            "work_exhausted": False,
        },
        path=tmp_path / "events.jsonl",
    )


def test_write_failure_reports_stderr_without_raising(tmp_path):
    stderr = io.StringIO()

    assert emit_event("server.started", {}, path=tmp_path, stderr=stderr) is False
    assert "operational event write failed" in stderr.getvalue()


def test_short_write_restores_the_exact_prior_jsonl_bytes(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    original = b'{"event":"previous"}\n'
    path.write_bytes(original)
    real_write = __import__("os").write

    def short_write(fd, data):
        real_write(fd, data[:-1])
        return len(data) - 1

    monkeypatch.setattr("llm_memory.observability.os.write", short_write)
    stderr = io.StringIO()

    assert emit_event("server.started", {}, path=path, stderr=stderr) is False
    assert path.read_bytes() == original
    assert "OSError" in stderr.getvalue()


def test_throwing_stderr_sink_does_not_escape_a_write_failure(tmp_path):
    class ThrowingSink:
        def write(self, _message):
            raise RuntimeError("stderr is closed")

    assert emit_event("server.started", {}, path=tmp_path, stderr=ThrowingSink()) is False
