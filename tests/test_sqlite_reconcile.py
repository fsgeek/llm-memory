from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.reconcile import WorkBudget
from llm_memory.sqlite_reconcile import reconcile_registry
from llm_memory.provider import ProviderUnavailable
from llm_memory.sqlite_store import SQLiteStateConflict, SQLiteStore


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def sqlite_store(tmp_path):
    store = SQLiteStore(tmp_path / "episodes.sqlite3", busy_timeout_ms=20)
    store.ensure()
    return store


def _taste(cycle: int, question: str, response: str) -> dict:
    return {"cycle": cycle, "user_message": question, "response_text": response}


def _claude(session: str, token: str, response: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": session,
        "uuid": token,
        "message": {"content": response},
    }


def _write_jsonl(path: Path, records: list[dict]) -> bytes:
    data = b"".join(
        json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
        for record in records
    )
    path.write_bytes(data)
    return data


def _enrollment(
    corpus_id: str,
    source_id: str,
    adapter: str,
    locator: Path,
    *,
    max_age: int = 3600,
    boundary_version: int = 1,
    canonicalization_version: int = 1,
) -> SourceEnrollment:
    return SourceEnrollment(
        corpus_id=corpus_id,
        source_id=source_id,
        adapter=adapter,
        boundary_version=boundary_version,
        canonicalization_version=canonicalization_version,
        locator=locator,
        enabled=True,
        full_validation_max_age_seconds=max_age,
    )


def _member(report) -> dict:
    return report.corpus_standing[0]["sources"][0]["members"][0]


def test_partial_build_persists_staging_but_not_active(sqlite_store, tmp_path):
    path = tmp_path / "bounded.jsonl"
    _write_jsonl(
        path,
        [
            _taste(1, "first bounded question", "first bounded response"),
            _taste(2, "second bounded question", "second bounded response"),
        ],
    )
    source = _enrollment("local", "synthetic", "taste_open_jsonl", path)

    report = reconcile_registry(
        sqlite_store, EnrollmentRegistry((source,)), WorkBudget(32, NOW)
    )

    assert _member(report)["freshness"] == "incomplete"
    assert report.work_exhausted is True
    assert report.bytes_read > 32
    assert sqlite_store.active_episode_refs("local", "synthetic") == ()
    assert sqlite_store.staging_episode_count("local", "synthetic") > 0


def test_bounded_build_resumes_and_activates_only_when_complete(
    sqlite_store, tmp_path
):
    path = tmp_path / "resume.jsonl"
    data = _write_jsonl(
        path,
        [
            _taste(1, "first resumable question", "first resumable response"),
            _taste(2, "second resumable question", "second resumable response"),
        ],
    )
    source = _enrollment("local", "resume", "taste_open_jsonl", path)
    registry = EnrollmentRegistry((source,))

    first = reconcile_registry(sqlite_store, registry, WorkBudget(32, NOW))
    second = reconcile_registry(sqlite_store, registry, WorkBudget(32, NOW))

    assert _member(first)["freshness"] == "incomplete"
    assert _member(second)["freshness"] == "current"
    assert _member(second)["indexed_through"]["value"] == len(data)
    assert _member(second)["integrity"]["chain_digest"]
    assert len(sqlite_store.active_episode_refs("local", "resume")) == 2
    assert sqlite_store.staging_episode_count("local", "resume") == 0


def test_append_seeds_in_database_and_charges_only_appended_source_bytes(
    sqlite_store, tmp_path
):
    path = tmp_path / "append.jsonl"
    _write_jsonl(path, [_taste(1, "first", "answer")])
    source = _enrollment("local", "append", "taste_open_jsonl", path)
    registry = EnrollmentRegistry((source,))
    reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))
    before = sqlite_store.member_state(source, source.source_id)
    appended = (
        json.dumps(_taste(2, "second", "reply"), separators=(",", ":")).encode()
        + b"\n"
    )
    with path.open("ab") as stream:
        stream.write(appended)

    report = reconcile_registry(
        sqlite_store, registry, WorkBudget(1_000_000, NOW)
    )

    after = sqlite_store.member_state(source, source.source_id)
    assert _member(report)["freshness"] == "current"
    assert report.bytes_read == len(appended)
    assert after["active_generation_id"] != before["active_generation_id"]
    assert _member(report)["database_work"]["seeded_episode_count"] == 1
    assert len(sqlite_store.active_episode_refs("local", "append")) == 2


def test_multi_member_source_set_keeps_independent_standing(sqlite_store, tmp_path):
    directory = tmp_path / "claude"
    directory.mkdir()
    first = _write_jsonl(
        directory / "a.jsonl", [_claude("session-a", "event-a", "first")]
    )
    second = _write_jsonl(
        directory / "b.jsonl", [_claude("session-b", "event-b", "second")]
    )
    source = _enrollment("local", "claude", "claude_code_jsonl", directory)

    report = reconcile_registry(
        sqlite_store, EnrollmentRegistry((source,)), WorkBudget(1_000_000, NOW)
    )

    members = report.corpus_standing[0]["sources"][0]["members"]
    assert len(members) == 2
    assert sorted(member["indexed_through"]["value"] for member in members) == [
        len(first),
        len(second),
    ]
    assert all(member["freshness"] == "current" for member in members)


def test_vanished_source_set_member_remains_visible(sqlite_store, tmp_path):
    directory = tmp_path / "vanished-member"
    directory.mkdir()
    first_path = directory / "a.jsonl"
    second_path = directory / "b.jsonl"
    _write_jsonl(first_path, [_claude("session-a", "event-a", "first")])
    _write_jsonl(second_path, [_claude("session-b", "event-b", "second")])
    source = _enrollment("local", "vanished", "claude_code_jsonl", directory)
    registry = EnrollmentRegistry((source,))
    reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))
    second_path.unlink()

    report = reconcile_registry(
        sqlite_store, registry, WorkBudget(1_000_000, NOW)
    )

    members = report.corpus_standing[0]["sources"][0]["members"]
    assert len(members) == 2
    vanished = next(
        member for member in members if member["source_standing"] == "missing"
    )
    assert vanished["freshness"] == "unavailable"


def test_exhausted_budget_still_reports_every_enrolled_corpus(
    sqlite_store, tmp_path
):
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    _write_jsonl(first_path, [_taste(1, "first", "answer")])
    _write_jsonl(second_path, [_taste(1, "second", "answer")])
    first = _enrollment("a-corpus", "first", "taste_open_jsonl", first_path)
    second = _enrollment("b-corpus", "second", "taste_open_jsonl", second_path)

    report = reconcile_registry(
        sqlite_store,
        EnrollmentRegistry((second, first)),
        WorkBudget(1, NOW),
    )

    assert report.work_exhausted is True
    assert [item["corpus_id"] for item in report.corpus_standing] == [
        "a-corpus",
        "b-corpus",
    ]
    remaining = report.corpus_standing[1]["sources"][0]["members"][0]
    assert remaining["source_standing"] == "unknown"
    assert remaining["freshness"] == "unknown"


def test_malformed_source_retains_nonsearchable_staging_prefix(
    sqlite_store, tmp_path
):
    path = tmp_path / "malformed.jsonl"
    valid = _write_jsonl(path, [_taste(1, "valid", "retained")])
    with path.open("ab") as stream:
        stream.write(b"{broken}\n")
    source = _enrollment("local", "malformed", "taste_open_jsonl", path)

    report = reconcile_registry(
        sqlite_store, EnrollmentRegistry((source,)), WorkBudget(1_000_000, NOW)
    )

    member = _member(report)
    assert member["source_standing"] == "malformed"
    assert member["freshness"] == "unknown"
    assert member["error_position"] == len(valid)
    assert sqlite_store.active_episode_refs("local", "malformed") == ()
    assert sqlite_store.staging_episode_count("local", "malformed") == 1


def test_unavailable_source_does_not_activate(sqlite_store, tmp_path):
    path = tmp_path / "missing.jsonl"
    source = _enrollment("local", "missing", "taste_open_jsonl", path)

    report = reconcile_registry(
        sqlite_store, EnrollmentRegistry((source,)), WorkBudget(1_000_000, NOW)
    )

    member = _member(report)
    assert member["source_standing"] == "missing"
    assert member["freshness"] == "unavailable"
    assert member["index_standing"] == "unavailable"
    assert report.bytes_read == 0
    assert sqlite_store.active_episode_refs("local", "missing") == ()


def test_available_empty_source_activates_as_exact_current(sqlite_store, tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    source = _enrollment("local", "empty", "taste_open_jsonl", path)

    report = reconcile_registry(
        sqlite_store, EnrollmentRegistry((source,)), WorkBudget(1_000_000, NOW)
    )

    member = _member(report)
    state = sqlite_store.member_state(source, source.source_id)
    assert member["source_standing"] == "available"
    assert member["index_standing"] == "available"
    assert member["freshness"] == "current"
    assert state["episode_count"] == 0
    assert state["validated_at"] is not None
    assert sqlite_store.active_episode_refs("local", "empty") == ()


def test_unsupported_semantic_version_is_zero_read_and_does_not_abort_registry(
    sqlite_store, tmp_path
):
    unsupported_path = tmp_path / "unsupported.jsonl"
    supported_path = tmp_path / "supported.jsonl"
    _write_jsonl(unsupported_path, [_taste(1, "unsupported", "version")])
    supported_bytes = _write_jsonl(
        supported_path, [_taste(1, "supported", "version")]
    )
    unsupported = _enrollment(
        "a-corpus",
        "unsupported",
        "taste_open_jsonl",
        unsupported_path,
        canonicalization_version=2,
    )
    supported = _enrollment(
        "b-corpus", "supported", "taste_open_jsonl", supported_path
    )

    report = reconcile_registry(
        sqlite_store,
        EnrollmentRegistry((unsupported, supported)),
        WorkBudget(1_000_000, NOW),
    )

    rejected = report.corpus_standing[0]["sources"][0]["members"][0]
    accepted = report.corpus_standing[1]["sources"][0]["members"][0]
    assert rejected["source_standing"] == "unsupported_adapter"
    assert rejected["freshness"] == "unavailable"
    assert rejected["index_standing"] == "unavailable"
    assert accepted["freshness"] == "current"
    assert report.bytes_read == len(supported_bytes)
    assert sqlite_store.staging_episode_count("a-corpus", "unsupported") == 0
    assert sqlite_store.active_episode_refs("a-corpus", "unsupported") == ()


def test_periodic_full_validation_is_bounded_and_resumable(sqlite_store, tmp_path):
    path = tmp_path / "audit.jsonl"
    data = _write_jsonl(
        path,
        [_taste(1, "first", "answer"), _taste(2, "second", "reply")],
    )
    source = _enrollment(
        "local", "audit", "taste_open_jsonl", path, max_age=10
    )
    registry = EnrollmentRegistry((source,))
    reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))

    partial = reconcile_registry(
        sqlite_store,
        registry,
        WorkBudget(1, NOW + timedelta(seconds=11)),
    )
    completed = reconcile_registry(
        sqlite_store,
        registry,
        WorkBudget(1, NOW + timedelta(seconds=11)),
    )

    assert _member(partial)["freshness"] == "tail_validated"
    assert 0 < _member(partial)["integrity"]["audit_offset"] < len(data)
    assert _member(completed)["freshness"] == "current"
    assert _member(completed)["integrity"]["audit_offset"] == len(data)
    assert len(sqlite_store.active_episode_refs("local", "audit")) == 2


def test_prefix_rewrite_keeps_old_active_until_replacement_completes(
    sqlite_store, tmp_path
):
    path = tmp_path / "rewrite.jsonl"
    _write_jsonl(
        path,
        [_taste(1, "first", "aaa"), _taste(2, "second", "bbb")],
    )
    source = _enrollment(
        "local", "rewrite", "taste_open_jsonl", path, max_age=10
    )
    registry = EnrollmentRegistry((source,))
    reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))
    original_refs = sqlite_store.active_episode_refs("local", "rewrite")
    original_generation = sqlite_store.member_state(source, source.source_id)[
        "active_generation_id"
    ]
    _write_jsonl(
        path,
        [_taste(1, "first", "ccc"), _taste(2, "second", "ddd")],
    )
    rewritten_bytes = path.read_bytes()

    detected = reconcile_registry(
        sqlite_store,
        registry,
        WorkBudget(1_000_000, NOW + timedelta(seconds=11)),
    )
    detected_state = sqlite_store.member_state(source, source.source_id)
    rebuilt = reconcile_registry(
        sqlite_store,
        registry,
        WorkBudget(1_000_000, NOW + timedelta(seconds=11)),
    )
    rebuilt_state = sqlite_store.member_state(source, source.source_id)
    rewritten_refs = sqlite_store.active_episode_refs("local", "rewrite")

    assert _member(detected)["freshness"] == "stale"
    assert detected_state["active_generation_id"] == original_generation
    assert rewritten_refs != original_refs
    assert _member(rebuilt)["freshness"] == "current"
    assert rebuilt_state["active_generation_id"] != original_generation
    assert [
        sqlite_store.resolve_supersession(source, old_ref) for old_ref in original_refs
    ] == list(rewritten_refs)
    assert path.read_bytes() == rewritten_bytes


def test_mid_record_truncation_never_replaces_active_generation(
    sqlite_store, tmp_path
):
    path = tmp_path / "truncated.jsonl"
    original = _write_jsonl(
        path,
        [_taste(1, "first", "answer"), _taste(2, "second", "reply")],
    )
    source = _enrollment(
        "local", "truncated", "taste_open_jsonl", path, max_age=10
    )
    registry = EnrollmentRegistry((source,))
    reconcile_registry(sqlite_store, registry, WorkBudget(1_000_000, NOW))
    original_refs = sqlite_store.active_episode_refs("local", "truncated")
    original_generation = sqlite_store.member_state(source, source.source_id)[
        "active_generation_id"
    ]
    path.write_bytes(original[: len(original) // 2])

    report = reconcile_registry(
        sqlite_store,
        registry,
        WorkBudget(1_000_000, NOW + timedelta(seconds=11)),
    )

    state = sqlite_store.member_state(source, source.source_id)
    assert _member(report)["freshness"] == "stale"
    assert state["active_generation_id"] == original_generation
    assert sqlite_store.active_episode_refs("local", "truncated") == original_refs


def test_single_state_conflict_retries_once(sqlite_store, tmp_path, monkeypatch):
    path = tmp_path / "one-conflict.jsonl"
    _write_jsonl(path, [_taste(1, "question", "answer")])
    source = _enrollment("local", "one-conflict", "taste_open_jsonl", path)
    original = sqlite_store.compare_and_swap_state
    attempts = 0

    def conflict_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SQLiteStateConflict("injected first conflict")
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite_store, "compare_and_swap_state", conflict_once)

    report = reconcile_registry(
        sqlite_store, EnrollmentRegistry((source,)), WorkBudget(1_000_000, NOW)
    )

    assert attempts >= 2
    assert _member(report)["freshness"] == "current"


def test_repeated_state_conflict_is_provider_unavailable(
    sqlite_store, tmp_path, monkeypatch
):
    path = tmp_path / "repeated-conflict.jsonl"
    _write_jsonl(path, [_taste(1, "question", "answer")])
    source = _enrollment(
        "local", "repeated-conflict", "taste_open_jsonl", path
    )
    attempts = 0

    def always_conflict(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise SQLiteStateConflict("injected repeated conflict")

    monkeypatch.setattr(sqlite_store, "compare_and_swap_state", always_conflict)

    with pytest.raises(ProviderUnavailable, match="did not converge"):
        reconcile_registry(
            sqlite_store,
            EnrollmentRegistry((source,)),
            WorkBudget(1_000_000, NOW),
        )

    assert attempts == 2


def test_supported_semantic_version_rebuilds_and_records_supersession(
    sqlite_store, tmp_path, enable_semantic_version
):
    path = tmp_path / "semantic-version.jsonl"
    _write_jsonl(path, [_taste(1, "question", "answer")])
    original = _enrollment(
        "local", "semantic-version", "taste_open_jsonl", path
    )
    reconcile_registry(
        sqlite_store,
        EnrollmentRegistry((original,)),
        WorkBudget(1_000_000, NOW),
    )
    old_ref = sqlite_store.active_episode_refs(
        "local", "semantic-version"
    )[0]
    enable_semantic_version("taste_open_jsonl", canonicalization=2)
    changed = _enrollment(
        "local",
        "semantic-version",
        "taste_open_jsonl",
        path,
        canonicalization_version=2,
    )

    report = reconcile_registry(
        sqlite_store,
        EnrollmentRegistry((changed,)),
        WorkBudget(1_000_000, NOW),
    )

    new_ref = sqlite_store.active_episode_refs("local", "semantic-version")[0]
    assert _member(report)["freshness"] == "current"
    assert new_ref != old_ref
    assert sqlite_store.resolve_supersession(changed, old_ref) == new_ref
