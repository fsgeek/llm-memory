from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Lock

import pytest

from llm_memory.adapters import get_adapter
from llm_memory.enrollment import EnrollmentRegistry
from llm_memory.provider import ProviderUnavailable
from llm_memory.reconcile import WorkBudget
from llm_memory.sqlite_provider import SQLiteProvider
from llm_memory.sqlite_store import SQLiteStore


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _primary_registry(synthetic_source):
    return EnrollmentRegistry((synthetic_source.source("primary"),))


def _assert_active_generations_are_complete(store: SQLiteStore) -> None:
    with store.read_transaction() as connection:
        states = connection.execute(
            "SELECT corpus_id, source_id, member_id, state_json "
            "FROM source_states ORDER BY corpus_id, source_id, member_id"
        ).fetchall()
        assert states
        for row in states:
            state = json.loads(row["state_json"])
            generation_id = state.get("active_generation_id")
            if generation_id is None:
                continue
            expected = state["episode_count"]
            documents = connection.execute(
                "SELECT count(*) FROM episode_documents "
                "WHERE corpus_id = ? AND source_id = ? AND member_id = ? "
                "AND generation_id = ?",
                (
                    row["corpus_id"],
                    row["source_id"],
                    row["member_id"],
                    generation_id,
                ),
            ).fetchone()[0]
            indexed = connection.execute(
                "SELECT count(*) FROM episode_fts WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()[0]
            assert state["active_generation_integrity"] == "valid"
            assert (documents, indexed) == (expected, expected)


def test_eight_parallel_sqlite_reconcilers_have_bounded_complete_outcomes(
    tmp_path, synthetic_source
):
    path = tmp_path / "parallel.sqlite3"
    registry = _primary_registry(synthetic_source)
    SQLiteProvider(path, busy_timeout_ms=100).ensure()
    barrier = Barrier(8)
    connection_lock = Lock()
    worker_connections = []

    def reconcile_from_independent_store(_):
        provider = SQLiteProvider(path, busy_timeout_ms=100)
        worker_connection = provider.store.connect()
        with connection_lock:
            worker_connections.append(worker_connection)
        try:
            barrier.wait(timeout=5)
            try:
                report = provider.reconcile(registry, WorkBudget(1_000_000, NOW))
            except ProviderUnavailable:
                return "retryable"
            member = report.corpus_standing[0]["sources"][0]["members"][0]
            assert member["index_standing"] == "available"
            return "available"
        finally:
            worker_connection.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(reconcile_from_independent_store, range(8)))

    assert len(outcomes) == 8
    assert len({id(connection) for connection in worker_connections}) == 8
    assert all(outcome in {"available", "retryable"} for outcome in outcomes)
    assert "available" in outcomes
    final_provider = SQLiteProvider(path, busy_timeout_ms=100)
    final = final_provider.reconcile(registry, WorkBudget(1_000_000, NOW))
    member = final.corpus_standing[0]["sources"][0]["members"][0]
    assert member["freshness"] == "current"
    adapter = get_adapter(registry.sources[0].adapter)
    expected_refs = {
        episode.identity.episode_ref
        for episode in adapter.scan(
            registry.sources[0], adapter.members(registry.sources[0])[0]
        ).episodes
    }
    assert set(
        final_provider.store.active_episode_refs(
            registry.sources[0].corpus_id, registry.sources[0].source_id
        )
    ) == expected_refs
    _assert_active_generations_are_complete(final_provider.store)
    assert synthetic_source.path.read_bytes() == synthetic_source.original_bytes


def test_begin_immediate_held_beyond_timeout_is_provider_unavailable(
    tmp_path, synthetic_source
):
    path = tmp_path / "locked.sqlite3"
    timeout_ms = 30
    blocker_store = SQLiteStore(path, busy_timeout_ms=timeout_ms)
    blocker_store.ensure()
    blocker = blocker_store.connect()
    blocker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(ProviderUnavailable, match="locked|busy"):
            SQLiteProvider(path, busy_timeout_ms=timeout_ms).reconcile(
                _primary_registry(synthetic_source),
                WorkBudget(1_000_000, NOW),
            )
    finally:
        elapsed = time.monotonic() - started
        blocker.rollback()
        blocker.close()

    assert elapsed >= timeout_ms / 1000
    assert elapsed < 2
    assert synthetic_source.path.read_bytes() == synthetic_source.original_bytes


def test_subprocess_crash_before_commit_leaves_no_partial_active_generation(
    tmp_path, synthetic_source
):
    path = tmp_path / "crash.sqlite3"
    store = SQLiteStore(path, busy_timeout_ms=100)
    store.ensure()
    crash_script = """
import json
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute("PRAGMA journal_mode = WAL")
connection.execute("BEGIN IMMEDIATE")
state = json.dumps({
    "corpus_id": "crash-corpus",
    "source_id": "crash-source",
    "member_id": "crash-member",
    "staging_generation_id": "crash-generation",
    "staging_episode_count": 1,
    "freshness": "incomplete"
}, sort_keys=True, separators=(",", ":"))
connection.execute(
    "INSERT INTO source_states(" 
    "state_key, corpus_id, source_id, member_id, revision, state_json" 
    ") VALUES (?, ?, ?, ?, ?, ?)",
    ("crash-state", "crash-corpus", "crash-source", "crash-member", 1, state),
)
connection.execute(
    "INSERT INTO episode_documents(" 
    "storage_key, corpus_id, source_id, member_id, generation_id, episode_ref, "
    "reference_key, timestamp, user_message, response, state_text, document_json" 
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    (
        "crash-storage", "crash-corpus", "crash-source", "crash-member",
        "crash-generation", "episode://crash/ref/value", "crash-reference", "",
        "crash question", "crash response", "", "{}"
    ),
)
os._exit(17)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", crash_script, str(path)],
        check=False,
        timeout=5,
    )

    assert crashed.returncode == 17
    with store.read_transaction() as connection:
        assert connection.execute(
            "SELECT count(*) FROM source_states WHERE corpus_id = 'crash-corpus'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM episode_documents "
            "WHERE generation_id = 'crash-generation'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM episode_fts "
            "WHERE generation_id = 'crash-generation'"
        ).fetchone()[0] == 0

    provider = SQLiteProvider(path, busy_timeout_ms=100)
    report = provider.reconcile(
        _primary_registry(synthetic_source), WorkBudget(1_000_000, NOW)
    )
    member = report.corpus_standing[0]["sources"][0]["members"][0]
    assert member["freshness"] == "current"
    _assert_active_generations_are_complete(provider.store)
    assert synthetic_source.path.read_bytes() == synthetic_source.original_bytes
