import hashlib
import io
import json
import stat

import pytest

import llm_memory.observability as observability
from llm_memory.contract import EpisodeReference


def _episode_ref(event_token: str = "0", *, session_length: int = 14) -> str:
    return str(
        EpisodeReference.build(
            corpus_id="codex-history",
            source_id="machine-uuid",
            native_session_id="s" * session_length,
            canonicalization_version=1,
            boundary_version=1,
            event_token=event_token,
            content_digest="0" * 64,
        )
    )


def _event_records(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(path))
    return path


def test_typed_open_event_writes_one_private_identifier_only_json_line(tmp_path, monkeypatch):
    path = _event_records(tmp_path, monkeypatch)

    assert observability.emit_open_event(
        corpus_ids=["codex-history"],
        episode_ref=_episode_ref(),
        standing="available",
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["event"] == "open.completed"
    assert record["standing"] == "available"
    assert record["episode_ref"] == _episode_ref()
    assert "ts" in record
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reconciliation_hashes_opaque_source_and_member_identifiers(tmp_path, monkeypatch):
    path = _event_records(tmp_path, monkeypatch)
    source_id = "password=hunter2"
    member_id = "secret member identifier"

    assert observability.emit_reconciliation_event(
        corpus_id="codex-history",
        source_id=source_id,
        member_id=member_id,
        source_standing="available",
        index_standing="available",
        episode_count=2,
        bytes_read=17,
        duration_ms=2.5,
        work_exhausted=False,
    )

    serialized = path.read_text(encoding="utf-8")
    record = json.loads(serialized)
    assert source_id not in serialized
    assert member_id not in serialized
    assert record["source_id"] == f"sha256:{hashlib.sha256(source_id.encode()).hexdigest()}"
    assert record["member_id"] == f"sha256:{hashlib.sha256(member_id.encode()).hexdigest()}"


def test_reconciliation_preserves_a_canonical_machine_uuid(tmp_path, monkeypatch):
    path = _event_records(tmp_path, monkeypatch)
    machine_uuid = "e8c598ae-711b-42b5-b963-eb35fc946d2b"

    assert observability.emit_reconciliation_event(
        corpus_id="codex-history",
        source_id=machine_uuid,
        member_id="member-1",
        source_standing="available",
        index_standing="available",
        episode_count=0,
        bytes_read=0,
        duration_ms=0,
        work_exhausted=False,
    )

    assert json.loads(path.read_text(encoding="utf-8"))["source_id"] == machine_uuid


def test_closed_initialization_and_empty_reconciliation_start_events(
    tmp_path, monkeypatch
):
    path = _event_records(tmp_path, monkeypatch)

    assert observability.emit_initialization_event(
        "provider", outcome="initialized"
    )
    assert observability.emit_initialization_event(
        "enrollment", outcome="missing"
    )
    assert observability.emit_reconciliation_started(
        corpus_count=0, source_count=0
    )

    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(record["event"], record.get("outcome")) for record in records] == [
        ("provider.initialized", "initialized"),
        ("enrollment.initialized", "missing"),
        ("reconcile.started", None),
    ]
    assert records[-1]["corpus_count"] == 0
    assert records[-1]["source_count"] == 0


@pytest.mark.parametrize(
    ("phase", "diagnostic_code"),
    [
        ("provider", "provider_initialization_failed"),
        ("enrollment", "enrollment_initialization_failed"),
        ("reconciliation", "reconciliation_failed"),
    ],
)
def test_startup_phase_failures_use_distinct_content_free_codes(
    tmp_path, monkeypatch, phase, diagnostic_code
):
    path = _event_records(tmp_path, monkeypatch)
    secret = "credential=do-not-persist"

    assert observability.emit_failure_event(phase, RuntimeError(secret))

    serialized = path.read_text(encoding="utf-8")
    record = json.loads(serialized)
    assert secret not in serialized
    assert record["event"] == f"{phase}.failed"
    assert record["exception_class"] == "RuntimeError"
    assert record["diagnostic_code"] == diagnostic_code


@pytest.mark.parametrize(
    "identifier",
    [
        "E8C598AE-711B-42B5-B963-EB35FC946D2B",
        "e8c598ae711b42b5b963eb35fc946d2b",
        " e8c598ae-711b-42b5-b963-eb35fc946d2b ",
        "0123456789abcdef0123456789abcdef",
    ],
)
def test_reconciliation_hashes_uuid_lookalikes_instead_of_normalizing_them(
    tmp_path, monkeypatch, identifier
):
    path = _event_records(tmp_path, monkeypatch)

    assert observability.emit_reconciliation_event(
        corpus_id="codex-history",
        source_id=identifier,
        member_id="member-1",
        source_standing="available",
        index_standing="available",
        episode_count=0,
        bytes_read=0,
        duration_ms=0,
        work_exhausted=False,
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["source_id"] == (
        f"sha256:{hashlib.sha256(identifier.encode()).hexdigest()}"
    )


def test_search_summarizes_one_hundred_near_limit_references_with_a_digest(tmp_path, monkeypatch):
    path = _event_records(tmp_path, monkeypatch)
    refs = [_episode_ref(str(index), session_length=800) for index in range(100)]

    assert observability.emit_search_event(
        corpus_ids=["codex-history"], returned_count=100, episode_refs=refs
    )

    serialized = path.read_text(encoding="utf-8")
    record = json.loads(serialized)
    assert record["returned_count"] == 100
    assert record["episode_refs_sha256"] == hashlib.sha256(
        "\n".join(refs).encode("utf-8")
    ).hexdigest()
    assert refs[0] not in serialized
    assert len(serialized.encode("utf-8")) < 8192


def test_failure_omits_malformed_identifiers_without_replacing_the_operation_error(tmp_path, monkeypatch):
    path = _event_records(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="secret operation body"):
        try:
            raise RuntimeError("secret operation body")
        except RuntimeError as exc:
            assert observability.emit_failure_event(
                "search",
                exc,
                corpus_ids=["malformed corpus id"],
                episode_ref="not-an-episode-reference",
            )
            raise

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record == {
        "event": "search.failed",
        "exception_class": "RuntimeError",
        "diagnostic_code": "contract_search_failed",
        "ts": record["ts"],
    }


def test_failure_keeps_valid_corpora_while_omitting_malformed_ones(tmp_path, monkeypatch):
    path = _event_records(tmp_path, monkeypatch)

    assert observability.emit_failure_event(
        "open",
        RuntimeError("secret operation body"),
        corpus_ids=["codex-history", "malformed corpus id"],
    )

    assert json.loads(path.read_text(encoding="utf-8"))["corpus_ids"] == ["codex-history"]


@pytest.mark.parametrize(
    "call",
    [
        lambda: observability.emit_server_event(object()),
        lambda: observability.emit_reconciliation_event(
            corpus_id="codex-history",
            source_id=object(),
            member_id="member-1",
            source_standing="available",
            index_standing="available",
            episode_count=0,
            bytes_read=0,
            duration_ms=0,
            work_exhausted=False,
        ),
        lambda: observability.emit_search_event(
            corpus_ids=["codex-history"], returned_count=True, episode_refs=[]
        ),
        lambda: observability.emit_open_event(
            corpus_ids=["codex-history"], episode_ref=object(), standing="available"
        ),
        lambda: observability.emit_failure_event("search", object()),
    ],
)
def test_typed_emitters_return_false_for_malformed_or_nonserializable_arguments(call):
    assert call() is False


def test_short_write_restores_the_exact_prior_jsonl_bytes(tmp_path, monkeypatch):
    path = _event_records(tmp_path, monkeypatch)
    original = b'{"event":"previous"}\n'
    path.write_bytes(original)
    real_write = __import__("os").write

    def short_write(fd, data):
        real_write(fd, data[:-1])
        return len(data) - 1

    monkeypatch.setattr("llm_memory.observability.os.write", short_write)

    assert observability.emit_server_event("started") is False
    assert path.read_bytes() == original


def test_throwing_stderr_sink_does_not_escape_a_write_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(tmp_path))

    class ThrowingSink:
        def write(self, _message):
            raise RuntimeError("stderr is closed")

    monkeypatch.setattr(observability.sys, "stderr", ThrowingSink())
    assert observability.emit_server_event("started") is False


def test_typed_emitter_suppresses_keyboard_interrupt_from_private_writer(monkeypatch):
    monkeypatch.setattr(
        observability,
        "_write_envelope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert observability.emit_server_event("started") is False


def test_private_writer_suppresses_keyboard_interrupt_from_dependency(monkeypatch):
    monkeypatch.setattr(
        observability.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert observability.emit_server_event("started") is False


def test_failure_report_suppresses_keyboard_interrupt_from_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(tmp_path))

    class InterruptingSink:
        def write(self, _message):
            raise KeyboardInterrupt()

    monkeypatch.setattr(observability.sys, "stderr", InterruptingSink())
    assert observability.emit_server_event("started") is False


def test_generic_mapping_entry_point_is_not_public():
    assert not hasattr(observability, "emit_event")
    assert not hasattr(observability, "event_log_path")
