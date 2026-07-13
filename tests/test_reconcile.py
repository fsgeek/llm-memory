from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import llm_memory.adapters as adapters_module
import llm_memory.reconcile as reconcile_module
from llm_memory.contract import EpisodeReference
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    SOURCE_STATES,
    SUPERSESSIONS,
    active_states,
    ensure_contract_index,
)
from llm_memory.db import get_database
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment
from llm_memory.reconcile import WorkBudget, extend_chain, reconcile_registry


NOW = datetime(2026, 7, 12, 18, 30, tzinfo=UTC)


@pytest.fixture
def reconciliation_storage():
    db = get_database()
    ensure_contract_index(db)
    prefix = f"reconcile-test-{uuid4().hex}"
    try:
        yield db, prefix
    finally:
        for collection_name in (CONTRACT_EPISODES, SOURCE_STATES, SUPERSESSIONS):
            db.aql.execute(
                """
                FOR doc IN @@collection
                    FILTER STARTS_WITH(doc.corpus_id, @prefix)
                    REMOVE doc IN @@collection
                """,
                bind_vars={"@collection": collection_name, "prefix": prefix},
            )


def enrollment(
    corpus_id: str,
    source_id: str,
    adapter: str,
    locator: Path,
    *,
    canonicalization_version: int = 1,
    boundary_version: int = 1,
    max_age: int = 3600,
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


def write_jsonl(path: Path, records: list[dict], *, final_newline: bool = True) -> bytes:
    data = b"\n".join(json.dumps(record).encode() for record in records)
    if final_newline:
        data += b"\n"
    path.write_bytes(data)
    return data


def taste(cycle: int, question: str, response: str) -> dict:
    return {"cycle": cycle, "user_message": question, "response_text": response}


def gateway(session: str, question: str, response: str) -> dict:
    return {
        "type": "request_metrics",
        "session_id": session,
        "messages_full": [{"role": "user", "content": question}],
        "response_text": response,
    }


def claude(session: str, token: str, response: str) -> dict:
    return {
        "type": "assistant",
        "sessionId": session,
        "uuid": token,
        "message": {"content": response},
    }


def run(db, *sources: SourceEnrollment, max_bytes: int = 1_000_000, now=NOW):
    return reconcile_registry(db, EnrollmentRegistry(tuple(sources)), WorkBudget(max_bytes, now))


def members(report) -> list[dict]:
    return [
        member
        for corpus in report.corpus_standing
        for source in corpus["sources"]
        for member in source["members"]
    ]


def documents(db, corpus_id: str) -> list[dict]:
    return list(
        db.aql.execute(
            """
            FOR doc IN @@episodes
                FILTER doc.corpus_id == @corpus_id
                SORT doc.source_id, doc.member_id, doc.source_position.start
                RETURN doc
            """,
            bind_vars={"@episodes": CONTRACT_EPISODES, "corpus_id": corpus_id},
        )
    )


def active_documents(db, corpus_id: str) -> list[dict]:
    active = {state["active_generation_id"] for state in active_states(db, (corpus_id,))}
    return [doc for doc in documents(db, corpus_id) if doc["generation_id"] in active]


def source_state(db, corpus_id: str, source_id: str = "taste") -> dict:
    return list(
        db.aql.execute(
            """
            FOR state IN @@states
                FILTER state.corpus_id == @corpus_id
                FILTER state.source_id == @source_id
                RETURN state
            """,
            bind_vars={
                "@states": SOURCE_STATES,
                "corpus_id": corpus_id,
                "source_id": source_id,
            },
        )
    )[0]


def test_work_budget_validation_and_initial_build_reaches_current(reconciliation_storage, tmp_path):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "taste.jsonl"
    data = write_jsonl(path, [taste(1, "one", "answer"), taste(2, "two", "reply")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)

    with pytest.raises(ValueError, match="positive"):
        WorkBudget(0, NOW)
    report = run(db, source)

    member = members(report)[0]
    source_report = report.corpus_standing[0]["sources"][0]
    assert source_report == source_report | {
        "source_id": "taste",
        "adapter": "taste_open_jsonl",
        "implementation_version": "1",
        "canonicalization_version": 1,
        "boundary_version": 1,
        "source_set_standing": "available",
    }
    assert "freshness" not in source_report
    assert member["source_standing"] == "available"
    assert member["index_standing"] == "available"
    assert member["freshness"] == "current"
    assert member["indexed_through"] == {"kind": "byte_offset", "value": len(data)}
    assert member["observed_source_end"] == {"kind": "byte_offset", "value": len(data)}
    assert member["integrity"]["basis"] == "full_digest"
    assert member["integrity"]["chain_digest"]
    assert member["integrity"]["audit_offset"] == len(data)
    assert len(active_documents(db, corpus_id)) == 2


def test_append_and_partial_or_malformed_tail_preserve_exact_position(reconciliation_storage, tmp_path):
    db, prefix = reconciliation_storage
    path = tmp_path / "tail.jsonl"
    first = write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(prefix, "taste", "taste_open_jsonl", path)
    initial = run(db, source)
    initial_generation = active_states(db, (prefix,))[0]["active_generation_id"]

    appended = json.dumps(taste(2, "two", "reply")).encode() + b"\n"
    with path.open("ab") as stream:
        stream.write(appended)
    append_report = run(db, source)
    append_state = active_states(db, (prefix,))[0]
    assert append_state["active_generation_id"] != initial_generation
    assert append_state["complete_end"] == len(first + appended)
    assert len(active_documents(db, prefix)) == 2
    assert members(append_report)[0]["freshness"] == "current"

    partial = json.dumps(taste(3, "partial", "ignored")).encode()
    with path.open("ab") as stream:
        stream.write(partial)
    partial_report = run(db, source)
    partial_member = members(partial_report)[0]
    assert partial_member["freshness"] == "incomplete"
    assert partial_member["indexed_through"]["value"] == len(first + appended)
    assert len(active_documents(db, prefix)) == 2

    malformed_path = tmp_path / "malformed.jsonl"
    valid = write_jsonl(malformed_path, [taste(1, "valid", "kept")])
    with malformed_path.open("ab") as stream:
        stream.write(b"{broken}\n")
    malformed_source = enrollment(prefix + "-bad", "taste", "taste_open_jsonl", malformed_path)
    malformed = members(run(db, malformed_source))[0]
    assert malformed["source_standing"] == "malformed"
    assert malformed["error_position"] == len(valid)
    assert len(active_documents(db, prefix + "-bad")) == 1


def test_source_sets_and_multiple_declarations_keep_independent_standing(reconciliation_storage, tmp_path):
    db, corpus_id = reconciliation_storage
    directory = tmp_path / "claude"
    directory.mkdir()
    first = directory / "a.jsonl"
    second = directory / "b.jsonl"
    first_data = write_jsonl(first, [claude("session-a", "a", "first")])
    second_data = write_jsonl(second, [claude("session-b", "b", "second")])
    gateway_path = tmp_path / "gateway.jsonl"
    write_jsonl(gateway_path, [gateway("gateway-session", "q", "a")])
    claude_source = enrollment(corpus_id, "claude", "claude_code_jsonl", directory)
    gateway_source = enrollment(corpus_id, "gateway", "gateway_jsonl", gateway_path)

    report = run(db, claude_source, gateway_source)

    corpus = report.corpus_standing[0]
    assert [source["source_id"] for source in corpus["sources"]] == ["claude", "gateway"]
    claude_members = corpus["sources"][0]["members"]
    assert sorted(m["indexed_through"]["value"] for m in claude_members) == sorted(
        [len(first_data), len(second_data)]
    )
    assert len({member["member_id"] for member in claude_members}) == 2


def test_relocation_preserves_references_and_bounded_work_resumes(reconciliation_storage, tmp_path):
    db, prefix = reconciliation_storage
    first = tmp_path / "first.jsonl"
    data = write_jsonl(first, [gateway("s", "one", "a"), gateway("s", "two", "b")])
    source = enrollment(prefix, "gateway", "gateway_jsonl", first)
    run(db, source)
    original_refs = [doc["episode_ref"] for doc in active_documents(db, prefix)]

    relocated = tmp_path / "relocated.jsonl"
    relocated.write_bytes(data)
    moved_source = replace(source, locator=relocated)
    run(db, moved_source)
    assert [doc["episode_ref"] for doc in active_documents(db, prefix)] == original_refs

    bounded_path = tmp_path / "bounded.jsonl"
    write_jsonl(bounded_path, [taste(1, "one", "a"), taste(2, "two", "b")])
    bounded_source = enrollment(prefix + "-bounded", "taste", "taste_open_jsonl", bounded_path)
    reports = []
    for _ in range(8):
        report = run(db, bounded_source, max_bytes=1)
        reports.append(report)
        if members(report)[0]["freshness"] == "current":
            break
    assert reports[0].work_exhausted is True
    assert reports[0].bytes_read > 1
    assert members(reports[0])[0]["freshness"] == "incomplete"
    assert members(reports[-1])[0]["freshness"] == "current"


def test_expired_validation_is_tail_validated_until_resumable_audit_finishes(reconciliation_storage, tmp_path):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "audit.jsonl"
    data = write_jsonl(path, [taste(1, "one", "a"), taste(2, "two", "b")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path, max_age=10)
    run(db, source)

    expired = run(db, source, max_bytes=1, now=NOW + timedelta(seconds=11))
    state = active_states(db, (corpus_id,))[0]
    member = members(expired)[0]
    assert member["freshness"] == "tail_validated"
    assert 0 < state["integrity_audit"]["offset"] < len(data)
    expected = extend_chain("", active_documents(db, corpus_id)[0]["episode_ref"])
    assert state["integrity_audit"]["chain_digest"] == expected

    completed = run(db, source, max_bytes=1, now=NOW + timedelta(seconds=11))
    assert members(completed)[0]["freshness"] == "current"


def test_tail_work_precedes_expired_audit_when_budget_is_spent(reconciliation_storage, tmp_path):
    db, corpus_id = reconciliation_storage
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    write_jsonl(first_path, [taste(1, "first", "a")])
    write_jsonl(second_path, [taste(1, "second", "b")])
    first = enrollment(corpus_id, "first", "taste_open_jsonl", first_path, max_age=10)
    second = enrollment(corpus_id, "second", "taste_open_jsonl", second_path)
    run(db, first, second)
    second_generation = next(
        state["active_generation_id"]
        for state in active_states(db, (corpus_id,))
        if state["source_id"] == "second"
    )
    with second_path.open("ab") as stream:
        stream.write(json.dumps(taste(2, "tail", "c")).encode() + b"\n")

    report = run(db, first, second, max_bytes=1, now=NOW + timedelta(seconds=11))
    states = {state["source_id"]: state for state in active_states(db, (corpus_id,))}

    assert states["second"]["active_generation_id"] != second_generation
    assert states["first"]["freshness"] == "tail_validated"
    assert states["first"]["integrity_audit"]["offset"] == 0
    assert report.work_exhausted is True


def test_audit_restarts_when_member_changes_during_scan(reconciliation_storage, tmp_path, monkeypatch):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "changing.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa"), taste(2, "two", "bbb")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path, max_age=10)
    run(db, source)
    delegate = adapters_module.get_adapter("taste_open_jsonl")

    class ChangingAdapter:
        name = delegate.name
        implementation_version = delegate.implementation_version

        def members(self, declared):
            return delegate.members(declared)

        def scan(self, declared, member):
            return delegate.scan(declared, member)

        def scan_chunk(self, declared, member, cursor, max_bytes):
            chunk = delegate.scan_chunk(declared, member, cursor, max_bytes)
            write_jsonl(path, [taste(1, "one", "ccc"), taste(2, "two", "ddd")])
            return chunk

    monkeypatch.setitem(adapters_module._ADAPTERS, "taste_open_jsonl", ChangingAdapter())

    report = run(db, source, now=NOW + timedelta(seconds=11))
    state = active_states(db, (corpus_id,))[0]

    assert members(report)[0]["freshness"] == "tail_validated"
    assert state["integrity_audit"]["offset"] == 0
    assert state["integrity_audit"]["restart_count"] >= 1


def test_prefix_rewrite_activates_replacement_after_full_audit(reconciliation_storage, tmp_path):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "rewrite.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa"), taste(2, "two", "bbb")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_generation = active_states(db, (corpus_id,))[0]["active_generation_id"]
    original_refs = [doc["episode_ref"] for doc in active_documents(db, corpus_id)]

    write_jsonl(path, [taste(1, "one", "ccc"), taste(2, "two", "ddd")])
    detected = run(db, source, now=NOW + timedelta(hours=2))
    assert members(detected)[0]["freshness"] == "stale"
    assert active_states(db, (corpus_id,))[0]["active_generation_id"] == original_generation

    rebuilt = run(db, source, now=NOW + timedelta(hours=2))
    assert members(rebuilt)[0]["freshness"] == "current"
    assert active_states(db, (corpus_id,))[0]["active_generation_id"] != original_generation
    assert [doc["episode_ref"] for doc in active_documents(db, corpus_id)] != original_refs


@pytest.mark.parametrize("version_field", ["canonicalization_version", "boundary_version"])
def test_version_changes_preserve_or_supersede_references(reconciliation_storage, tmp_path, monkeypatch, version_field):
    db, prefix = reconciliation_storage
    path = tmp_path / "versions.jsonl"
    write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(prefix, "taste", "taste_open_jsonl", path)
    run(db, source)
    original = active_documents(db, prefix)[0]["episode_ref"]
    adapter = adapters_module.get_adapter("taste_open_jsonl")
    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        replace(adapter, implementation_version="2"),
    )

    implementation = run(db, source)
    assert members(implementation)[0]["freshness"] == "current"
    assert active_documents(db, prefix)[0]["episode_ref"] == original

    semantic_source = replace(source, **{version_field: 2})
    semantic = run(db, semantic_source)
    assert members(semantic)[0]["freshness"] == "current"
    replacement = active_documents(db, prefix)[0]["episode_ref"]
    assert replacement != original
    supersessions = list(
        db.aql.execute(
            "FOR doc IN @@collection FILTER doc.corpus_id == @corpus RETURN doc",
            bind_vars={"@collection": SUPERSESSIONS, "corpus": prefix},
        )
    )
    assert [(doc["old_ref"], doc["new_ref"]) for doc in supersessions] == [(original, replacement)]
    assert getattr(EpisodeReference.parse(replacement), version_field) == 2


def test_vanished_source_set_member_remains_visible(reconciliation_storage, tmp_path):
    db, corpus_id = reconciliation_storage
    directory = tmp_path / "claude"
    directory.mkdir()
    path = directory / "session.jsonl"
    write_jsonl(path, [claude("session-a", "answer", "present")])
    source = enrollment(corpus_id, "claude", "claude_code_jsonl", directory)
    initial = run(db, source)
    member_id = members(initial)[0]["member_id"]

    path.unlink()
    report = run(db, source)

    member = members(report)[0]
    assert member["member_id"] == member_id
    assert member["source_standing"] in {"missing", "unavailable"}
    assert len(active_documents(db, corpus_id)) == 1


@pytest.mark.parametrize("failure", ["truncated", "missing", "malformed"])
def test_invalid_source_cannot_recertify_stale_active_generation(
    reconciliation_storage, tmp_path, failure
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "invalid-audit.jsonl"
    first = json.dumps(taste(1, "one", "aaa")).encode() + b"\n"
    second = json.dumps(taste(2, "two", "bbb")).encode() + b"\n"
    path.write_bytes(first + second)
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path, max_age=10)
    run(db, source)

    if failure == "truncated":
        path.write_bytes(first)
    elif failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"{broken}\n")
    report = run(db, source, now=NOW + timedelta(seconds=11))

    assert members(report)[0]["freshness"] != "current"

    path.write_bytes(first + second)
    recovered = run(db, source, now=NOW + timedelta(seconds=11))
    assert members(recovered)[0]["freshness"] == "current"


def test_audit_completion_compares_full_active_chain_and_count(
    reconciliation_storage, tmp_path
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "full-chain.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa"), taste(2, "two", "bbb")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path, max_age=10)
    run(db, source)
    run(db, source, max_bytes=1, now=NOW + timedelta(seconds=11))
    first_document = active_documents(db, corpus_id)[0]
    db.collection(CONTRACT_EPISODES).delete(first_document["_key"])

    completed = run(db, source, now=NOW + timedelta(seconds=11))

    assert members(completed)[0]["freshness"] != "current"


def test_semantic_version_change_restarts_in_progress_bounded_build(
    reconciliation_storage, tmp_path
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "semantic-mid-build.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa"), taste(2, "two", "bbb")])
    first_version = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, first_version, max_bytes=1)
    abandoned_generation = source_state(db, corpus_id)["staging_generation_id"]

    second_version = replace(first_version, canonicalization_version=2)
    report = run(db, second_version)

    assert members(report)[0]["freshness"] == "current"
    state = active_states(db, (corpus_id,))[0]
    assert state["canonicalization_version"] == 2
    assert state["active_generation_id"] != abandoned_generation
    assert all(
        document["generation_id"] != abandoned_generation
        for document in documents(db, corpus_id)
    )


def test_retry_after_insert_before_staging_state_is_idempotent(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "insert-failure.jsonl"
    write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    collection_type = type(db.collection(CONTRACT_EPISODES))
    original_insert = collection_type.insert
    failed = False

    def insert_then_fail(self, document, *args, **kwargs):
        nonlocal failed
        result = original_insert(self, document, *args, **kwargs)
        if document.get("corpus_id") == corpus_id and not failed:
            failed = True
            raise RuntimeError("injected after insert")
        return result

    monkeypatch.setattr(collection_type, "insert", insert_then_fail)
    with pytest.raises(RuntimeError, match="injected"):
        run(db, source)
    monkeypatch.setattr(collection_type, "insert", original_insert)

    resumed = run(db, source)

    assert members(resumed)[0]["freshness"] == "current"
    assert len(active_documents(db, corpus_id)) == 1


@pytest.mark.parametrize("boundary", ["seeded", "cursor"])
def test_bounded_build_state_patch_retries_are_idempotent(
    reconciliation_storage, tmp_path, monkeypatch, boundary
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / f"{boundary}.jsonl"
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    if boundary == "seeded":
        write_jsonl(path, [taste(1, "one", "aaa")])
        run(db, source)
        with path.open("ab") as stream:
            stream.write(json.dumps(taste(2, "two", "bbb")).encode() + b"\n")
    else:
        write_jsonl(path, [taste(1, "one", "aaa"), taste(2, "two", "bbb")])
    original_patch = reconcile_module._patch_state
    failed = False

    def patch_then_fail(database, declared, member_id, values):
        nonlocal failed
        target = (
            values.get("build_seeded") is True
            if boundary == "seeded"
            else "build_cursor" in values and "observed_end" in values
        )
        if target and not failed:
            failed = True
            raise RuntimeError(f"injected before {boundary} state patch")
        return original_patch(database, declared, member_id, values)

    monkeypatch.setattr(reconcile_module, "_patch_state", patch_then_fail)
    with pytest.raises(RuntimeError, match="injected"):
        run(db, source, max_bytes=1)
    monkeypatch.setattr(reconcile_module, "_patch_state", original_patch)

    for _ in range(6):
        resumed = run(db, source, max_bytes=1)
        if members(resumed)[0]["freshness"] == "current":
            break

    assert members(resumed)[0]["freshness"] == "current"
    assert len(active_documents(db, corpus_id)) == 2


def test_supersession_finalization_resumes_after_activation_failure(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "supersession-resume.jsonl"
    write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    old_generation = active_states(db, (corpus_id,))[0]["active_generation_id"]
    changed = replace(source, canonicalization_version=2)
    original_finalize = reconcile_module._record_supersessions
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected after activation")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(reconcile_module, "_record_supersessions", fail_once)
    with pytest.raises(RuntimeError, match="injected"):
        run(db, changed)
    monkeypatch.setattr(reconcile_module, "_record_supersessions", original_finalize)

    resumed = run(db, changed)
    supersessions = list(
        db.aql.execute(
            "FOR doc IN @@collection FILTER doc.corpus_id == @corpus RETURN doc",
            bind_vars={"@collection": SUPERSESSIONS, "corpus": corpus_id},
        )
    )

    assert len(supersessions) == 1
    assert all(
        document["generation_id"] != old_generation
        for document in documents(db, corpus_id)
    )
    assert source_state(db, corpus_id).get("supersession_finalization") is None


def test_supersession_intent_does_not_finalize_before_activation(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "supersession-before-activation.jsonl"
    write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    changed = replace(source, boundary_version=2)

    def fail_activation(*args, **kwargs):
        raise RuntimeError("injected before activation")

    monkeypatch.setattr(reconcile_module, "activate_generation", fail_activation)
    with pytest.raises(RuntimeError, match="injected"):
        run(db, changed)
    with pytest.raises(RuntimeError, match="injected"):
        run(db, changed)
    supersessions = list(
        db.aql.execute(
            "FOR doc IN @@collection FILTER doc.corpus_id == @corpus RETURN doc",
            bind_vars={"@collection": SUPERSESSIONS, "corpus": corpus_id},
        )
    )

    assert supersessions == []


def test_gateway_supersessions_match_native_session_and_event_token(
    reconciliation_storage, tmp_path
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "gateway-sessions.jsonl"
    write_jsonl(path, [gateway("session-a", "one", "aaa"), gateway("session-b", "two", "bbb")])
    source = enrollment(corpus_id, "gateway", "gateway_jsonl", path)
    run(db, source)

    changed = replace(source, boundary_version=2)
    run(db, changed)
    supersessions = list(
        db.aql.execute(
            "FOR doc IN @@collection FILTER doc.corpus_id == @corpus RETURN doc",
            bind_vars={"@collection": SUPERSESSIONS, "corpus": corpus_id},
        )
    )

    assert len(supersessions) == 2
    assert all(
        EpisodeReference.parse(document["old_ref"]).native_session_id
        == EpisodeReference.parse(document["new_ref"]).native_session_id
        for document in supersessions
    )
