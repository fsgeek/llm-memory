from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import llm_memory.adapters as adapters_module
import llm_memory.reconcile as reconcile_module
from llm_memory.contract import EpisodeReference, build_identity
from llm_memory.contract_index import (
    CONTRACT_EPISODES,
    SOURCE_STATES,
    SUPERSESSIONS,
    active_states,
    activate_generation,
    ensure_contract_index,
    write_generation,
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


class ChangedOutputAdapter:
    def __init__(self, delegate, implementation_version: str, on_chunk=None):
        self.delegate = delegate
        self.name = delegate.name
        self.implementation_version = implementation_version
        self.on_chunk = on_chunk

    def members(self, declared):
        return self.delegate.members(declared)

    def scan(self, declared, member):
        return self.delegate.scan(declared, member)

    def scan_chunk(self, declared, member, cursor, max_bytes):
        chunk = self.delegate.scan_chunk(declared, member, cursor, max_bytes)
        changed = []
        for episode in chunk.episodes:
            body = replace(episode.body, response=episode.body.response + " changed")
            reference = episode.identity.reference
            changed.append(
                replace(
                    episode,
                    body=body,
                    identity=build_identity(
                        corpus_id=declared.corpus_id,
                        source_id=declared.source_id,
                        native_session_id=reference.native_session_id,
                        event_token=reference.event_token,
                        canonicalization_version=declared.canonicalization_version,
                        boundary_version=declared.boundary_version,
                        body=body,
                    ),
                )
            )
        result = replace(chunk, episodes=tuple(changed))
        if self.on_chunk:
            self.on_chunk(result)
        return result


class InterleavingAdapter:
    def __init__(self, delegate, on_chunk):
        self.delegate = delegate
        self.name = delegate.name
        self.implementation_version = delegate.implementation_version
        self.on_chunk = on_chunk

    def members(self, declared):
        return self.delegate.members(declared)

    def scan(self, declared, member):
        return self.delegate.scan(declared, member)

    def scan_chunk(self, declared, member, cursor, max_bytes):
        chunk = self.delegate.scan_chunk(declared, member, cursor, max_bytes)
        self.on_chunk(chunk)
        return chunk


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
def test_version_changes_preserve_or_supersede_references(
    reconciliation_storage,
    tmp_path,
    monkeypatch,
    enable_semantic_version,
    version_field,
):
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

    versions = {"boundary": 1, "canonicalization": 1}
    versions[version_field.removesuffix("_version")] = 2
    enable_semantic_version("taste_open_jsonl", **versions)
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


def test_missing_active_episode_triggers_replacement_rebuild(
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

    assert members(completed)[0]["freshness"] == "current"
    assert len(active_documents(db, corpus_id)) == 2


def test_semantic_version_change_restarts_in_progress_bounded_build(
    reconciliation_storage, tmp_path, enable_semantic_version
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "semantic-mid-build.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa"), taste(2, "two", "bbb")])
    first_version = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, first_version, max_bytes=1)
    abandoned_generation = source_state(db, corpus_id)["staging_generation_id"]

    enable_semantic_version("taste_open_jsonl", canonicalization=2)
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

    def patch_then_fail(database, declared, member_id, values, **kwargs):
        nonlocal failed
        target = (
            values.get("build_seeded") is True
            if boundary == "seeded"
            else "build_cursor" in values and "observed_end" in values
        )
        if target and not failed:
            failed = True
            raise RuntimeError(f"injected before {boundary} state patch")
        return original_patch(database, declared, member_id, values, **kwargs)

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
    reconciliation_storage, tmp_path, monkeypatch, enable_semantic_version
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "supersession-resume.jsonl"
    write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    old_generation = active_states(db, (corpus_id,))[0]["active_generation_id"]
    enable_semantic_version("taste_open_jsonl", canonicalization=2)
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
    reconciliation_storage, tmp_path, monkeypatch, enable_semantic_version
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "supersession-before-activation.jsonl"
    write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    enable_semantic_version("taste_open_jsonl", boundary=2)
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
    reconciliation_storage, tmp_path, enable_semantic_version
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "gateway-sessions.jsonl"
    write_jsonl(path, [gateway("session-a", "one", "aaa"), gateway("session-b", "two", "bbb")])
    source = enrollment(corpus_id, "gateway", "gateway_jsonl", path)
    run(db, source)

    enable_semantic_version("gateway_jsonl", boundary=2)
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


def test_incompatible_implementation_audit_preserves_active_references(
    reconciliation_storage, tmp_path, monkeypatch, enable_semantic_version
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "incompatible-implementation.jsonl"
    write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_state = active_states(db, (corpus_id,))[0]
    original_refs = [document["episode_ref"] for document in active_documents(db, corpus_id)]
    delegate = adapters_module.get_adapter("taste_open_jsonl")
    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        ChangedOutputAdapter(delegate, "2"),
    )

    first = run(db, source)
    second = run(db, source)

    assert members(first)[0]["index_standing"] == "unavailable"
    assert members(second)[0]["index_standing"] == "unavailable"
    assert active_states(db, (corpus_id,))[0]["active_generation_id"] == original_state[
        "active_generation_id"
    ]
    assert [document["episode_ref"] for document in active_documents(db, corpus_id)] == original_refs
    assert list(
        db.aql.execute(
            "FOR doc IN @@supersessions FILTER doc.corpus_id == @corpus RETURN doc",
            bind_vars={"@supersessions": SUPERSESSIONS, "corpus": corpus_id},
        )
    ) == []

    with path.open("ab") as stream:
        stream.write(json.dumps(taste(2, "two", "appended")).encode() + b"\n")
    still_incompatible = run(db, source)

    assert members(still_incompatible)[0]["index_standing"] == "unavailable"
    assert active_states(db, (corpus_id,))[0]["active_generation_id"] == original_state[
        "active_generation_id"
    ]
    assert [
        document["episode_ref"] for document in active_documents(db, corpus_id)
    ] == original_refs

    enable_semantic_version("taste_open_jsonl", canonicalization=2)
    semantic_change = replace(source, canonicalization_version=2)
    rebuilt = run(db, semantic_change)

    assert members(rebuilt)[0]["index_standing"] == "available"
    assert active_states(db, (corpus_id,))[0]["active_generation_id"] != original_state[
        "active_generation_id"
    ]
    assert [
        document["episode_ref"] for document in active_documents(db, corpus_id)
    ] != original_refs


def test_implementation_change_validates_indexed_prefix_before_append(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "implementation-and-append.jsonl"
    write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_state = active_states(db, (corpus_id,))[0]
    original_refs = [document["episode_ref"] for document in active_documents(db, corpus_id)]
    with path.open("ab") as stream:
        stream.write(json.dumps(taste(2, "two", "appended")).encode() + b"\n")
    delegate = adapters_module.get_adapter("taste_open_jsonl")
    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        ChangedOutputAdapter(delegate, "2"),
    )

    report = run(db, source)
    state = source_state(db, corpus_id)

    assert members(report)[0]["index_standing"] == "unavailable"
    assert members(report)[0]["freshness"] == "unknown"
    assert state["active_generation_id"] == original_state["active_generation_id"]
    assert state["implementation_version"] == delegate.implementation_version
    assert state.get("build_generation_id") is None
    assert [document["episode_ref"] for document in active_documents(db, corpus_id)] == original_refs


def test_compatible_implementation_validates_prefix_then_appends(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "compatible-implementation-and-append.jsonl"
    write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_generation = active_states(db, (corpus_id,))[0]["active_generation_id"]
    with path.open("ab") as stream:
        stream.write(json.dumps(taste(2, "two", "appended")).encode() + b"\n")
    delegate = adapters_module.get_adapter("taste_open_jsonl")
    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        replace(delegate, implementation_version="2"),
    )

    report = run(db, source)
    state = active_states(db, (corpus_id,))[0]

    assert members(report)[0]["index_standing"] == "available"
    assert members(report)[0]["freshness"] == "current"
    assert state["active_generation_id"] != original_generation
    assert state["implementation_version"] == "2"
    assert len(active_documents(db, corpus_id)) == 2


def test_compatible_prefix_validation_is_bounded_and_resumable(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "bounded-compatible-prefix.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa"), taste(2, "two", "bbb")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_generation = active_states(db, (corpus_id,))[0]["active_generation_id"]
    with path.open("ab") as stream:
        stream.write(json.dumps(taste(3, "three", "ccc")).encode() + b"\n")
    delegate = adapters_module.get_adapter("taste_open_jsonl")
    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        replace(delegate, implementation_version="2"),
    )

    bounded = run(db, source, max_bytes=1)
    bounded_state = source_state(db, corpus_id)

    assert bounded.work_exhausted is True
    assert members(bounded)[0]["index_standing"] == "unavailable"
    assert bounded_state["active_generation_id"] == original_generation
    assert bounded_state["implementation_version"] == delegate.implementation_version
    assert bounded_state["implementation_compatibility"] == "pending"
    assert bounded_state["integrity_audit"]["offset"] > 0

    resumed = run(db, source)
    state = active_states(db, (corpus_id,))[0]

    assert members(resumed)[0]["freshness"] == "current"
    assert state["implementation_version"] == "2"
    assert len(active_documents(db, corpus_id)) == 3


def test_compatibility_prefix_progress_survives_repeated_tail_growth(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "growing-compatible-prefix.jsonl"
    write_jsonl(
        path,
        [
            taste(1, "one", "aaa"),
            taste(2, "two", "bbb"),
            taste(3, "three", "ccc"),
        ],
    )
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_refs = [document["episode_ref"] for document in active_documents(db, corpus_id)]
    trusted_end = source_state(db, corpus_id)["complete_end"]
    delegate = adapters_module.get_adapter("taste_open_jsonl")
    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        replace(delegate, implementation_version="2"),
    )

    offsets = []
    for cycle in range(4, 7):
        bounded = run(db, source, max_bytes=1)
        state = source_state(db, corpus_id)
        offsets.append(state["integrity_audit"]["offset"])
        assert state["integrity_audit"]["target_end"] == trusted_end
        with path.open("ab") as stream:
            stream.write(
                json.dumps(taste(cycle, f"tail-{cycle}", f"answer-{cycle}")).encode()
                + b"\n"
            )
        if state["implementation_version"] == delegate.implementation_version:
            assert members(bounded)[0]["index_standing"] == "unavailable"

    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)
    assert offsets[-1] == trusted_end

    completed = run(db, source)
    active = active_documents(db, corpus_id)

    assert members(completed)[0]["freshness"] == "current"
    assert len(active) == 6
    assert [document["episode_ref"] for document in active[:3]] == original_refs


def test_compatibility_prefix_mutation_restarts_and_quarantines(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "mutated-compatible-prefix.jsonl"
    original_records = [
        taste(1, "one", "aaa"),
        taste(2, "two", "bbb"),
        taste(3, "three", "ccc"),
    ]
    write_jsonl(path, original_records)
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_generation = active_states(db, (corpus_id,))[0]["active_generation_id"]
    delegate = adapters_module.get_adapter("taste_open_jsonl")
    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        replace(delegate, implementation_version="2"),
    )
    run(db, source, max_bytes=1)
    prior_stat = path.stat()
    write_jsonl(path, [taste(1, "one", "xxx"), *original_records[1:]])
    os.utime(
        path,
        ns=(prior_stat.st_atime_ns, prior_stat.st_mtime_ns + 1_000_000),
    )

    report = run(db, source)
    state = source_state(db, corpus_id)

    assert members(report)[0]["index_standing"] == "unavailable"
    assert state["implementation_compatibility"] == "incompatible"
    assert state["integrity_audit"]["restart_count"] >= 1
    assert state["active_generation_id"] == original_generation


def test_compatibility_completion_publishes_tail_from_final_stat(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "compatibility-completion-window.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_state = active_states(db, (corpus_id,))[0]
    original_ref = active_documents(db, corpus_id)[0]["episode_ref"]
    trusted_end = source_state(db, corpus_id)["complete_end"]
    delegate = replace(
        adapters_module.get_adapter("taste_open_jsonl"),
        implementation_version="2",
    )
    appended = False

    def append_after_scan(_chunk):
        nonlocal appended
        if appended:
            return
        appended = True
        with path.open("ab") as stream:
            stream.write(json.dumps(taste(2, "tail", "bbb")).encode() + b"\n")

    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        InterleavingAdapter(delegate, append_after_scan),
    )

    bounded = run(db, source, max_bytes=1)
    state = source_state(db, corpus_id)
    final_stat = path.stat()

    assert bounded.work_exhausted is True
    assert members(bounded)[0]["freshness"] == "tail_validated"
    assert members(bounded)[0]["indexed_through"]["value"] == trusted_end
    assert members(bounded)[0]["observed_source_end"]["value"] == final_stat.st_size
    assert state["complete_end"] == trusted_end
    assert state["observed_end"] == final_stat.st_size
    assert state["member_generation"] == {
        "size": final_stat.st_size,
        "mtime_ns": final_stat.st_mtime_ns,
        "device": final_stat.st_dev,
        "inode": final_stat.st_ino,
    }
    assert state["active_generation_id"] == original_state["active_generation_id"]
    assert active_documents(db, corpus_id)[0]["episode_ref"] == original_ref

    completed = run(db, source, max_bytes=1_000_000)
    active = active_documents(db, corpus_id)

    assert members(completed)[0]["freshness"] == "current"
    assert len(active) == 2
    assert active[0]["episode_ref"] == original_ref


def test_pending_compatibility_tail_truncation_replaces_from_zero(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "pending-tail-truncated.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_generation = active_states(db, (corpus_id,))[0]["active_generation_id"]
    delegate = replace(
        adapters_module.get_adapter("taste_open_jsonl"),
        implementation_version="2",
    )
    appended = False

    def append_after_scan(_chunk):
        nonlocal appended
        if not appended:
            appended = True
            with path.open("ab") as stream:
                stream.write(json.dumps(taste(2, "tail", "bbb")).encode() + b"\n")

    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        InterleavingAdapter(delegate, append_after_scan),
    )
    pending = run(db, source, max_bytes=1)
    assert members(pending)[0]["freshness"] == "tail_validated"
    path.write_bytes(b"")

    recovered = run(db, source, max_bytes=1_000_000)
    state = active_states(db, (corpus_id,))[0]

    assert members(recovered)[0]["freshness"] == "current"
    assert members(recovered)[0]["index_standing"] == "available"
    assert state["active_generation_id"] != original_generation
    assert state["episode_count"] == 0


def test_pending_compatibility_tail_atomic_replacement_does_not_seed_old_prefix(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "pending-tail-replaced.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_ref = active_documents(db, corpus_id)[0]["episode_ref"]
    delegate = replace(
        adapters_module.get_adapter("taste_open_jsonl"),
        implementation_version="2",
    )
    appended = False

    def append_after_scan(_chunk):
        nonlocal appended
        if not appended:
            appended = True
            with path.open("ab") as stream:
                stream.write(json.dumps(taste(2, "tail", "bbb")).encode() + b"\n")

    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        InterleavingAdapter(delegate, append_after_scan),
    )
    run(db, source, max_bytes=1)
    pending_state = source_state(db, corpus_id)
    replacement = tmp_path / "replacement.jsonl"
    write_jsonl(
        replacement,
        [
            taste(10, "replacement-one", "xxx"),
            taste(11, "replacement-two", "yyy"),
            taste(12, "replacement-three", "zzz"),
        ],
    )
    assert replacement.stat().st_size > pending_state["complete_end"]
    os.replace(replacement, path)

    recovered = run(db, source, max_bytes=1_000_000)
    active = active_documents(db, corpus_id)

    assert members(recovered)[0]["freshness"] == "current"
    assert members(recovered)[0]["index_standing"] == "available"
    assert len(active) == 3
    assert original_ref not in {document["episode_ref"] for document in active}
    assert source_state(db, corpus_id)["complete_end"] == path.stat().st_size


def test_pending_compatibility_tail_disappearance_is_unavailable_then_recovers(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "pending-tail-missing.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_ref = active_documents(db, corpus_id)[0]["episode_ref"]
    delegate = replace(
        adapters_module.get_adapter("taste_open_jsonl"),
        implementation_version="2",
    )
    appended = False

    def append_after_scan(_chunk):
        nonlocal appended
        if not appended:
            appended = True
            with path.open("ab") as stream:
                stream.write(json.dumps(taste(2, "tail", "bbb")).encode() + b"\n")

    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        InterleavingAdapter(delegate, append_after_scan),
    )
    run(db, source, max_bytes=1)
    path.unlink()

    missing = run(db, source, max_bytes=1_000_000)
    missing_state = source_state(db, corpus_id)

    assert members(missing)[0]["source_standing"] == "missing"
    assert members(missing)[0]["index_standing"] == "unavailable"
    assert members(missing)[0]["freshness"] == "unavailable"
    assert missing_state["active_generation_integrity"] == "invalid"
    assert missing_state["build_reason"] == "pending_source_change"

    write_jsonl(path, [taste(20, "replacement", "xxx")])
    recovered = run(db, source, max_bytes=1_000_000)
    active = active_documents(db, corpus_id)

    assert members(recovered)[0]["freshness"] == "current"
    assert len(active) == 1
    assert active[0]["episode_ref"] != original_ref


def test_implementation_change_checks_trusted_prefix_before_derived_loss(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "implementation-and-derived-loss.jsonl"
    write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source)
    original_state = active_states(db, (corpus_id,))[0]
    for document in active_documents(db, corpus_id):
        db.collection(CONTRACT_EPISODES).delete(document["_key"])
    delegate = adapters_module.get_adapter("taste_open_jsonl")
    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        ChangedOutputAdapter(delegate, "2"),
    )

    report = run(db, source)
    state = source_state(db, corpus_id)

    assert members(report)[0]["index_standing"] == "unavailable"
    assert state["active_generation_id"] == original_state["active_generation_id"]
    assert state["implementation_version"] == delegate.implementation_version
    assert state.get("build_generation_id") is None


@pytest.mark.parametrize("source_kind", ["missing", "malformed"])
def test_initial_invalid_source_does_not_activate_empty_generation(
    reconciliation_storage, tmp_path, source_kind
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / f"{source_kind}.jsonl"
    if source_kind == "malformed":
        path.write_bytes(b"{broken}\n")
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)

    report = run(db, source)

    assert active_states(db, (corpus_id,)) == ()
    assert members(report)[0]["index_standing"] == "unavailable"


def test_initial_available_empty_source_is_exact_empty_generation(
    reconciliation_storage, tmp_path
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)

    report = run(db, source)

    state = active_states(db, (corpus_id,))[0]
    assert state["episode_count"] == 0
    assert members(report)[0]["index_standing"] == "available"


def test_stale_audit_cannot_certify_competing_replacement(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "audit-cas.jsonl"
    write_jsonl(path, [taste(1, "one", "answer")])
    source = enrollment(
        corpus_id, "taste", "taste_open_jsonl", path, max_age=10
    )
    run(db, source)
    delegate = adapters_module.get_adapter("taste_open_jsonl")
    member = delegate.members(source)[0]
    scan = delegate.scan(source, member)
    write_generation(db, source, member, "competing", scan.episodes)
    interleaved = False

    def activate_competing(_chunk):
        nonlocal interleaved
        if interleaved:
            return
        interleaved = True
        stat = path.stat()
        activate_generation(
            db,
            source,
            member,
            "competing",
            {
                "implementation_version": delegate.implementation_version,
                "observed_end": stat.st_size,
                "complete_end": stat.st_size,
                "member_generation": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
                "source_standing": "available",
                "freshness": "stale",
                "validated_at": None,
                "integrity_audit": {"offset": 0, "chain_digest": ""},
            },
        )

    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        InterleavingAdapter(delegate, activate_competing),
    )

    run(db, source, now=NOW + timedelta(seconds=11))
    state = active_states(db, (corpus_id,))[0]

    assert state["active_generation_id"] == "competing"
    assert state["freshness"] == "stale"
    assert state.get("validated_at") is None


def test_stale_build_cannot_overwrite_competing_build_progress(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "build-cas.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa"), taste(2, "two", "bbb")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source, max_bytes=1)
    delegate = adapters_module.get_adapter("taste_open_jsonl")
    advanced = False

    def advance_competing_build(_chunk):
        nonlocal advanced
        if advanced:
            return
        advanced = True
        state = source_state(db, corpus_id)
        reconcile_module._patch_state(
            db,
            source,
            "taste",
            {
                "build_generation_id": "competing-build",
                "build_cursor": {"byte_offset": 0, "adapter_state": {}},
            },
        )

    monkeypatch.setitem(
        adapters_module._ADAPTERS,
        "taste_open_jsonl",
        InterleavingAdapter(delegate, advance_competing_build),
    )

    run(db, source)
    state = source_state(db, corpus_id)

    assert state.get("active_generation_id") is None
    assert state["build_generation_id"] == "competing-build"


def test_stale_writer_cannot_publish_staging_after_competing_build_takes_ownership(
    reconciliation_storage, tmp_path, monkeypatch
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "staging-publication-cas.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa"), taste(2, "two", "bbb")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source, max_bytes=1)
    original_write = reconcile_module.write_generation
    interleaved = False

    def publish_after_competitor(database, declared, member, generation_id, episodes, **kwargs):
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            reconcile_module._patch_state(
                db,
                source,
                "taste",
                {
                    "build_generation_id": "competing-build",
                    "build_cursor": {"byte_offset": 0, "adapter_state": {}},
                    "staging_generation_id": "competing-stage",
                    "staging_episode_count": 0,
                },
            )
        return original_write(
            database,
            declared,
            member,
            generation_id,
            episodes,
            **kwargs,
        )

    monkeypatch.setattr(reconcile_module, "write_generation", publish_after_competitor)

    run(db, source)
    state = source_state(db, corpus_id)

    assert state["build_generation_id"] == "competing-build"
    assert state["build_cursor"] == {"byte_offset": 0, "adapter_state": {}}
    assert state["staging_generation_id"] == "competing-stage"
    assert state["staging_episode_count"] == 0


def test_zero_document_replacement_recovers_after_abandoned_staging_and_crash(
    reconciliation_storage, tmp_path, monkeypatch, enable_semantic_version
):
    db, corpus_id = reconciliation_storage
    path = tmp_path / "zero-replacement-recovery.jsonl"
    write_jsonl(path, [taste(1, "one", "aaa"), taste(2, "two", "bbb")])
    source = enrollment(corpus_id, "taste", "taste_open_jsonl", path)
    run(db, source, max_bytes=1)
    abandoned = source_state(db, corpus_id)["staging_generation_id"]
    path.write_bytes(b"")
    enable_semantic_version("taste_open_jsonl", canonicalization=2)
    changed = replace(source, canonicalization_version=2)
    original_activate = reconcile_module.activate_generation
    failed = False

    def crash_before_activation(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected zero-generation activation crash")
        return original_activate(*args, **kwargs)

    monkeypatch.setattr(reconcile_module, "activate_generation", crash_before_activation)
    with pytest.raises(RuntimeError, match="zero-generation activation crash"):
        run(db, changed)
    monkeypatch.setattr(reconcile_module, "activate_generation", original_activate)

    recovered = run(db, changed)
    state = active_states(db, (corpus_id,))[0]

    assert members(recovered)[0]["freshness"] == "current"
    assert state["episode_count"] == 0
    assert state["staging_generation_id"] is None
    assert state["active_generation_id"] != abandoned
    assert all(
        document["generation_id"] != abandoned
        for document in documents(db, corpus_id)
    )
