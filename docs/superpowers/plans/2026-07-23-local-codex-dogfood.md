# Local Codex Episodic-Memory Dog-Food Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a restarted Codex instance search this machine's current native Codex rollout and open a matched episode from the authoritative JSONL source through `llm-memory` and ArangoDB.

**Architecture:** A Linux collector feeds a platform-independent UUID normalizer, and that UUID becomes the durable enrollment `source_id`. A native `codex_jsonl` adapter converts only `session_meta` and conversational `event_msg` records into the existing episodic contract. Identifier-only JSONL events observe MCP lifecycle, reconciliation, search, and opening without copying conversation content.

**Tech Stack:** Python 3.14, stdlib `uuid`/`json`/`os`/`pathlib`, PyYAML, FastMCP, python-arango, pytest 9.1.

## Global Constraints

- Work in `/home/tony/projects/llm-memory`; preserve unrelated changes in `/home/tony/projects/qhaway`.
- Run tests with `uv run python -m pytest`; `config/db-config.ini` is present and points at the test-owned `llm_memory` ArangoDB.
- Use TDD for every code task: failing test, observed failure, minimal implementation, focused pass, full regression pass.
- The authoritative Codex JSONL is read-only input and must never be edited.
- The canonical machine identity is a lowercase, hyphenated UUID; never fall back to hostname, a random UUID, or another unstable identifier.
- Implement Linux collection only. Do not add Windows/macOS collection or machine-configuration capture.
- Parse only Codex `session_meta` and conversational `event_msg` records. Ignore `response_item`, tool traffic, context injections, and world state.
- Persistent events may contain identifiers, standings, counts, timings, and allowlisted diagnostic codes only—never prose, snippets, source bodies, arbitrary exception messages, credentials, or DB configuration.
- Keep event files user-private (`0o600`). An event-write failure reports to stderr but never replaces or falsifies the operation result.
- Do not add recursive session discovery, LAN serving, ranking/vector work, or `qhaway` integration.
- Commit source/tests in `llm-memory`; keep `config/sources.yaml`, `config/db-config.ini`, the event log, and qhaway's machine-local `.codex/config.toml` uncommitted.

---

## File Structure

- Create `llm_memory/machine_identity.py`: collect Linux's native machine ID and normalize any platform collector output to one UUID representation.
- Modify `llm_memory/adapters.py`: add the focused native Codex adapter and register it.
- Modify `llm_memory/adapter_versions.py`: register `codex_jsonl` semantic version 1/1.
- Modify `llm_memory/enrollment.py`: allow `codex_jsonl` enrollment.
- Create `llm_memory/observability.py`: append validated identifier-only operational events.
- Modify `llm_memory/reconcile.py`: expose internal episode counts in member standing for event summaries.
- Modify `llm_memory/mcp_server.py`: emit lifecycle, reconciliation, search, open, and controlled error events.
- Create `tests/test_machine_identity.py`: identity collection/normalization contract.
- Modify `tests/test_adapters.py`: native Codex parsing and bounded-resume contract.
- Modify `tests/test_enrollment.py`: native adapter enrollment and semantic-version validation.
- Create `tests/test_observability.py`: safe event schema, permissions, and failure behavior.
- Modify `tests/test_mcp_server.py`: identifier-only instrumentation and error-path behavior.
- Modify `tests/test_reconcile.py`: member report includes an episode count without changing public search output.
- Create ignored local `config/sources.yaml`: enroll this exact live rollout with the normalized machine UUID.
- Create machine-local `/home/tony/projects/qhaway/.codex/config.toml`: register the `llm-memory` stdio MCP server for this trusted project.

---

### Task 1: Normalize Stable Linux Machine Identity

**Files:**
- Create: `llm_memory/machine_identity.py`
- Create: `tests/test_machine_identity.py`

**Interfaces:**
- Produces: `normalize_machine_uuid(raw: str) -> str`
- Produces: `linux_machine_uuid(path: Path = Path("/etc/machine-id")) -> str`
- Consumes: only stdlib `uuid.UUID` and `pathlib.Path`.

- [ ] **Step 1: Write the failing normalization and collection tests**

```python
# tests/test_machine_identity.py
from pathlib import Path

import pytest

from llm_memory.machine_identity import linux_machine_uuid, normalize_machine_uuid


def test_normalizes_platform_identifier_to_canonical_uuid():
    assert normalize_machine_uuid("E8C598AE711B42B5B963EB35FC946D2B\n") == (
        "e8c598ae-711b-42b5-b963-eb35fc946d2b"
    )


@pytest.mark.parametrize("raw", ["", "not-a-uuid", "00000000000000000000000000000000"])
def test_rejects_missing_malformed_and_nil_identifiers(raw):
    with pytest.raises(ValueError, match="machine UUID"):
        normalize_machine_uuid(raw)


def test_linux_collector_reads_then_normalizes(tmp_path):
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("E8C598AE711B42B5B963EB35FC946D2B\n", encoding="utf-8")

    assert linux_machine_uuid(machine_id) == (
        "e8c598ae-711b-42b5-b963-eb35fc946d2b"
    )


def test_linux_collector_does_not_fall_back_when_file_is_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        linux_machine_uuid(tmp_path / "missing-machine-id")
```

- [ ] **Step 2: Run the focused tests and observe the missing module**

Run: `uv run python -m pytest tests/test_machine_identity.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'llm_memory.machine_identity'`.

- [ ] **Step 3: Implement the collector/normalizer boundary**

```python
# llm_memory/machine_identity.py
from __future__ import annotations

import uuid
from pathlib import Path


def normalize_machine_uuid(raw: str) -> str:
    if not isinstance(raw, str):
        raise TypeError("machine UUID must be a string")
    try:
        value = uuid.UUID(raw.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("machine UUID must be a valid UUID") from exc
    if value.int == 0:
        raise ValueError("machine UUID must not be nil")
    return str(value)


def linux_machine_uuid(path: Path = Path("/etc/machine-id")) -> str:
    return normalize_machine_uuid(path.read_text(encoding="utf-8"))
```

- [ ] **Step 4: Run focused and full tests**

Run: `uv run python -m pytest tests/test_machine_identity.py -v`

Expected: 6 tests pass.

Run: `uv run python -m pytest -q`

Expected: 426 passed and 1 existing skip, plus the 6 new passes.

- [ ] **Step 5: Commit the identity boundary**

```bash
git add llm_memory/machine_identity.py tests/test_machine_identity.py
git commit -m "feat: normalize stable machine identity"
```

---

### Task 2: Parse Native Codex Rollouts Into Episodic Records

**Files:**
- Modify: `llm_memory/adapters.py`
- Modify: `llm_memory/adapter_versions.py`
- Modify: `llm_memory/enrollment.py`
- Modify: `tests/test_adapters.py`
- Modify: `tests/test_enrollment.py`

**Interfaces:**
- Consumes: existing `SourceAdapter`, `SourceEnrollment`, `ScanCursor`, `_file_member`, `_scan_lines_chunk`, `_member_scan`, `_record`.
- Produces: immutable `CodexAdapter` with `name = "codex_jsonl"` and `implementation_version = "1"`.
- Produces cursor state keys: `native_session_id: str | None`, `latest_user: str`, `latest_user_ts: str`, `sequence_by_session: dict[str, int]`, `recognized_conversation: bool`.
- Produces one `EpisodeRecord` for each nonempty `event_msg.payload.agent_message`, paired with the latest nonempty user message.

- [ ] **Step 1: Add failing registry, enrollment, and native-record tests**

Extend the adapter registry expectation in `tests/test_adapters.py` with `"codex_jsonl": "1"`, then add:

```python
def codex_records():
    return [
        {
            "timestamp": "2026-07-22T17:50:42Z",
            "type": "session_meta",
            "payload": {"session_id": "native-codex-session"},
        },
        {
            "timestamp": "2026-07-22T17:51:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "wander with me"},
        },
        {
            "timestamp": "2026-07-22T17:51:01Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec"},
        },
        {
            "timestamp": "2026-07-22T17:51:02Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "we can wander"},
        },
        {
            "timestamp": "2026-07-22T17:51:03Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "and remain honest"},
        },
    ]


def test_codex_uses_session_meta_and_clean_conversation_events(tmp_path):
    path = tmp_path / "rollout.jsonl"
    data = write_jsonl(path, codex_records())

    result = scan(path, "codex_jsonl", source_id="machine-uuid")
    first, second = result.episodes

    assert result.source_standing is SourceStanding.AVAILABLE
    assert result.observed_end == result.complete_end == len(data)
    assert [episode.body.user_message for episode in result.episodes] == [
        "wander with me",
        "wander with me",
    ]
    assert [episode.body.response for episode in result.episodes] == [
        "we can wander",
        "and remain honest",
    ]
    assert [episode.identity.reference.native_session_id for episode in result.episodes] == [
        "native-codex-session",
        "native-codex-session",
    ]
    assert [episode.identity.reference.event_token for episode in result.episodes] == ["0", "1"]
    assert all(episode.native_event_id is None for episode in result.episodes)


def test_codex_ignores_blank_and_nonconversation_events(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(
        path,
        codex_records()
        + [
            {"type": "world_state", "payload": {"state": "private"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "  "}},
        ],
    )

    result = scan(path, "codex_jsonl")

    assert len(result.episodes) == 2
    assert all(episode.body.state == {} for episode in result.episodes)
    assert all(episode.body.activity_log == [] for episode in result.episodes)
    assert all(episode.body.adapter_fields == {} for episode in result.episodes)
```

Add `codex_jsonl` 1/1 to the semantic-version parameterization and add an enrollment fixture assertion in `tests/test_enrollment.py`:

```python
def test_registry_accepts_native_codex_adapter(tmp_path):
    source = VALID_CONFIG["sources"][0] | {
        "source_id": "e8c598ae-711b-42b5-b963-eb35fc946d2b",
        "adapter": "codex_jsonl",
        "locator": "/tmp/codex-rollout.jsonl",
    }

    registry = load_registry(write_config(tmp_path, VALID_CONFIG | {"sources": [source]}))

    assert registry.sources[0].adapter == "codex_jsonl"
```

- [ ] **Step 2: Run focused tests and observe registry rejection**

Run: `uv run python -m pytest tests/test_adapters.py -k codex -v tests/test_enrollment.py -k codex -v`

Expected: failures report unsupported adapter `codex_jsonl`.

- [ ] **Step 3: Register semantic version and enrollment name**

Add to `SUPPORTED_SEMANTIC_VERSIONS` in `llm_memory/adapter_versions.py`:

```python
"codex_jsonl": frozenset({(1, 1)}),
```

Add `"codex_jsonl"` to `_SUPPORTED_ADAPTERS` in `llm_memory/enrollment.py`.

- [ ] **Step 4: Implement the native adapter**

Add before `_ADAPTERS` in `llm_memory/adapters.py`:

```python
@dataclass(frozen=True)
class CodexAdapter:
    name: str = "codex_jsonl"
    implementation_version: str = "1"

    def members(self, enrollment: SourceEnrollment) -> tuple[SourceMember, ...]:
        return _file_member(enrollment)

    def scan(self, enrollment: SourceEnrollment, member: SourceMember) -> MemberScan:
        return _member_scan(self.scan_chunk(enrollment, member, None, 2**63 - 1))

    def scan_chunk(
        self,
        enrollment: SourceEnrollment,
        member: SourceMember,
        cursor: ScanCursor | None,
        max_bytes: int,
    ) -> MemberChunk:
        _require_semantic_versions(enrollment)
        state = dict(cursor.adapter_state) if cursor is not None else {}
        native_session_id = state.get("native_session_id")
        latest_user = state.get("latest_user", "")
        latest_user_ts = state.get("latest_user_ts", "")
        sequence_by_session = dict(state.get("sequence_by_session", {}))
        recognized_conversation = state.get("recognized_conversation", False)

        def handle(record: dict[str, Any], start: int, end: int) -> EpisodeRecord | None:
            nonlocal native_session_id, latest_user, latest_user_ts, recognized_conversation
            payload = record.get("payload")
            if not isinstance(payload, dict):
                return None
            if record.get("type") == "session_meta":
                session = payload.get("session_id")
                if not isinstance(session, str) or not session:
                    raise ValueError("Codex session_meta session_id is required")
                native_session_id = session
                state["native_session_id"] = session
                return None
            if record.get("type") != "event_msg":
                return None
            event_type = payload.get("type")
            message = payload.get("message")
            if event_type not in {"user_message", "agent_message"}:
                return None
            if not isinstance(message, str):
                raise ValueError("Codex conversational message must be a string")
            if not message.strip():
                return None
            if not isinstance(native_session_id, str) or not native_session_id:
                raise ValueError("Codex conversation requires prior session_meta")
            recognized_conversation = True
            state["recognized_conversation"] = True
            if event_type == "user_message":
                latest_user = message
                latest_user_ts = record.get("timestamp") or ""
                state["latest_user"] = latest_user
                state["latest_user_ts"] = latest_user_ts
                return None
            sequence = sequence_by_session.get(native_session_id, 0)
            body = EpisodeBody(
                timestamp=_optional_text(record, "timestamp"),
                model="",
                user_message=latest_user,
                response=message,
                state={},
                activity_log=[],
                adapter_fields={},
            )
            episode = _record(
                enrollment,
                native_session_id=native_session_id,
                event_token=str(sequence),
                body=body,
                native_event_id=None,
                start=start,
                end=end,
            )
            sequence_by_session[native_session_id] = sequence + 1
            state["sequence_by_session"] = sequence_by_session
            return episode

        result = _scan_lines_chunk(member, cursor, max_bytes, handle, state)
        if (
            (not native_session_id or not recognized_conversation)
            and result.source_standing is SourceStanding.AVAILABLE
            and not result.exhausted
            and result.freshness is not FreshnessStanding.INCOMPLETE
        ):
            return MemberChunk(
                member=result.member,
                episodes=(),
                next_cursor=result.next_cursor,
                observed_end=result.observed_end,
                complete_end=result.complete_end,
                source_standing=SourceStanding.MALFORMED,
                freshness=FreshnessStanding.UNKNOWN,
                bytes_read=result.bytes_read,
                exhausted=result.exhausted,
                error_position=0,
            )
        return result
```

Register `"codex_jsonl": CodexAdapter()` in `_ADAPTERS`.

- [ ] **Step 5: Add bounded-resume and honesty-guard tests**

```python
def test_codex_bounded_resume_matches_full_scan(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, codex_records())
    declared = enrollment("codex_jsonl", path)
    adapter = get_adapter(declared.adapter)
    member = adapter.members(declared)[0]
    full = adapter.scan(declared, member)
    cursor = None
    episodes = []
    while cursor is None or cursor.byte_offset < path.stat().st_size:
        chunk = adapter.scan_chunk(declared, member, cursor, 1)
        episodes.extend(chunk.episodes)
        assert chunk.source_standing is SourceStanding.AVAILABLE
        cursor = chunk.next_cursor
    assert [episode.identity.episode_ref for episode in episodes] == [
        episode.identity.episode_ref for episode in full.episodes
    ]


@pytest.mark.parametrize(
    "records",
    [
        [{"timestamp": "2026-07-22T00:00:00Z", "type": "world_state", "payload": {}}],
        [{"timestamp": "2026-07-22T00:00:00Z", "type": "session_meta", "payload": {"session_id": "s"}}],
    ],
)
def test_codex_clean_lookalike_without_conversation_is_malformed(tmp_path, records):
    path = tmp_path / "rollout.jsonl"
    write_jsonl(path, records)

    result = scan(path, "codex_jsonl")

    assert result.source_standing is SourceStanding.MALFORMED
    assert result.episodes == ()
    assert result.error_position == 0
```

- [ ] **Step 6: Run focused and full tests**

Run: `uv run python -m pytest tests/test_adapters.py tests/test_enrollment.py -v`

Expected: all adapter/enrollment tests pass, including native Codex and bounded resume.

Run: `uv run python -m pytest -q`

Expected: complete suite passes with the one existing skip.

- [ ] **Step 7: Commit the native adapter**

```bash
git add llm_memory/adapters.py llm_memory/adapter_versions.py llm_memory/enrollment.py tests/test_adapters.py tests/test_enrollment.py
git commit -m "feat: adapt native Codex rollout history"
```

---

### Task 3: Persist Identifier-Only Operational Events

**Files:**
- Create: `llm_memory/observability.py`
- Create: `tests/test_observability.py`

**Interfaces:**
- Produces: `event_log_path(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path`
- Produces: `emit_event(event: str, fields: Mapping[str, object], *, path: Path | None = None, stderr: TextIO | None = None) -> bool`
- Environment override: `LLM_MEMORY_EVENT_LOG`; default `~/.local/state/llm-memory/events.jsonl`.
- Accepted field names are a closed allowlist; nested content is not accepted.

- [ ] **Step 1: Write failing schema, permission, and failure-isolation tests**

```python
# tests/test_observability.py
import json
import stat

import pytest

from llm_memory.observability import emit_event, event_log_path


def test_default_and_environment_event_paths(tmp_path):
    assert event_log_path({}, home=tmp_path) == tmp_path / ".local/state/llm-memory/events.jsonl"
    assert event_log_path({"LLM_MEMORY_EVENT_LOG": "/tmp/custom-events.jsonl"}, home=tmp_path).as_posix() == "/tmp/custom-events.jsonl"


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


@pytest.mark.parametrize("forbidden", ["query", "snippet", "body", "response", "exception_message", "password"])
def test_rejects_content_or_secret_fields(tmp_path, forbidden):
    with pytest.raises(ValueError, match="event field"):
        emit_event("search.completed", {forbidden: "do not persist"}, path=tmp_path / "events.jsonl")


def test_write_failure_reports_stderr_without_raising(tmp_path):
    stderr = __import__("io").StringIO()
    assert emit_event("server.started", {}, path=tmp_path, stderr=stderr) is False
    assert "operational event write failed" in stderr.getvalue()


def test_nonserializable_allowed_value_does_not_break_the_operation(tmp_path):
    stderr = __import__("io").StringIO()
    assert emit_event(
        "server.started",
        {"outcome": object()},
        path=tmp_path / "events.jsonl",
        stderr=stderr,
    ) is False
    assert "TypeError" in stderr.getvalue()
```

- [ ] **Step 2: Run tests and observe the missing module**

Run: `uv run python -m pytest tests/test_observability.py -v`

Expected: `ModuleNotFoundError` for `llm_memory.observability`.

- [ ] **Step 3: Implement the closed event envelope and append-only writer**

```python
# llm_memory/observability.py
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, TextIO


_ALLOWED_FIELDS = frozenset(
    {
        "adapter",
        "bytes_read",
        "corpus_ids",
        "diagnostic_code",
        "duration_ms",
        "episode_count",
        "episode_ref",
        "episode_refs",
        "exception_class",
        "index_standing",
        "member_id",
        "operation_id",
        "outcome",
        "provider",
        "returned_count",
        "source_id",
        "source_standing",
        "standing",
        "work_exhausted",
    }
)


def event_log_path(
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    environ = os.environ if environ is None else environ
    configured = environ.get("LLM_MEMORY_EVENT_LOG")
    if configured:
        return Path(configured)
    return (Path.home() if home is None else home) / ".local/state/llm-memory/events.jsonl"


def emit_event(
    event: str,
    fields: Mapping[str, object],
    *,
    path: Path | None = None,
    stderr: TextIO | None = None,
) -> bool:
    if not isinstance(event, str) or not event:
        raise ValueError("event must be a nonempty string")
    unknown = set(fields) - _ALLOWED_FIELDS
    if unknown:
        raise ValueError(f"unsupported operational event field: {sorted(unknown)[0]}")
    record = {
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "event": event,
        **fields,
    }
    target = event_log_path() if path is None else path
    stderr = sys.stderr if stderr is None else stderr
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except (OSError, TypeError) as exc:
        stderr.write(f"operational event write failed: {type(exc).__name__}\n")
        return False
    return True
```

- [ ] **Step 4: Run focused and full tests**

Run: `uv run python -m pytest tests/test_observability.py -v`

Expected: all observability tests pass.

Run: `uv run python -m pytest -q`

Expected: complete suite passes with the one existing skip.

- [ ] **Step 5: Commit the event writer**

```bash
git add llm_memory/observability.py tests/test_observability.py
git commit -m "feat: persist identifier-only operational events"
```

---

### Task 4: Instrument Reconciliation and MCP Contract Operations

**Files:**
- Modify: `llm_memory/reconcile.py`
- Modify: `llm_memory/mcp_server.py`
- Modify: `tests/test_reconcile.py`
- Modify: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `emit_event(event, fields) -> bool` from Task 3.
- Produces: internal member standing field `episode_count: int`.
- Emits: `server.starting`, `server.started`, `server.stopped`, `reconcile.completed`, `search.completed`, `search.failed`, `open.completed`, `open.failed`, and `server.failed`.
- Error fields contain `exception_class` plus fixed `diagnostic_code`; they never contain exception text.

- [ ] **Step 1: Add a failing reconciliation episode-count assertion**

In the smallest existing successful reconcile test in `tests/test_reconcile.py`, add:

```python
member = report.corpus_standing[0]["sources"][0]["members"][0]
assert member["episode_count"] == 1
```

Run: `uv run python -m pytest tests/test_reconcile.py -k initial_build -v`

Expected: `KeyError: 'episode_count'`.

- [ ] **Step 2: Expose the internal count without changing public search output**

Add to `_member_standing()`'s returned dictionary in `llm_memory/reconcile.py`:

```python
"episode_count": state.get("episode_count", 0),
```

Run: `uv run python -m pytest tests/test_reconcile.py -k initial_build -v`

Expected: focused test passes.

Run: `uv run python -m pytest tests/test_history_search.py tests/test_sqlite_history.py -v`

Expected: public response tests remain unchanged because `_public_member()` does not expose the internal count.

- [ ] **Step 3: Add failing MCP event tests using an injected recorder**

Add to `tests/test_mcp_server.py`:

```python
def test_contract_lifespan_and_tools_emit_identifier_only_events(monkeypatch):
    registry = object()
    provider = RecordingProvider()
    provider.reconcile = lambda registry, budget: type(
        "Report",
        (),
        {
            "corpus_standing": (
                {
                    "corpus_id": "codex-history",
                    "sources": (
                        {
                            "source_id": "machine-uuid",
                            "members": ({"member_id": "member-1", "episode_count": 2, "source_standing": "available", "index_standing": "available"},),
                        },
                    ),
                },
            ),
            "bytes_read": 17,
            "elapsed_ms": 2.5,
            "work_exhausted": False,
        },
    )()
    provider.search = lambda registry, request, budget: {
        "returned_count": 1,
        "results": [{"episode_ref": "episode://codex-history/session/episode"}],
    }
    events = []
    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)
    monkeypatch.setattr(mcp_server, "load_registry", lambda: registry)
    monkeypatch.setattr(mcp_server, "emit_event", lambda event, fields: events.append((event, fields)) or True)
    monkeypatch.setattr(mcp_server, "_open_episode", lambda *args: {"episode_ref": args[1], "standing": "available", "response": "must not log"})

    def operations():
        mcp_server.search_history("secret query", ["codex-history"])
        mcp_server.open_episode("episode://codex-history/session/episode", ["codex-history"])

    _run_in_lifespan(operations)

    serialized = json.dumps(events)
    assert "secret query" not in serialized
    assert "must not log" not in serialized
    assert {event for event, _ in events} >= {
        "server.starting", "reconcile.completed", "server.started",
        "search.completed", "open.completed", "server.stopped",
    }
    assert next(fields for event, fields in events if event == "search.completed")["episode_refs"] == ["episode://codex-history/session/episode"]


def test_contract_error_event_uses_class_and_code_not_message(monkeypatch):
    events = []
    monkeypatch.setattr(mcp_server, "emit_event", lambda event, fields: events.append((event, fields)) or True)
    monkeypatch.setattr(mcp_server, "_contract_runtime", lambda: (_ for _ in ()).throw(RuntimeError("secret body")))

    with pytest.raises(RuntimeError, match="secret body"):
        mcp_server.search_history("secret query", ["codex-history"])

    assert events == [
        (
            "search.failed",
            {"exception_class": "RuntimeError", "diagnostic_code": "contract_search_failed", "corpus_ids": ["codex-history"]},
        )
    ]
    assert "secret body" not in json.dumps(events)


def test_malformed_reconciliation_persists_identifier_only_standing(tmp_path, monkeypatch):
    registry = object()
    provider = RecordingProvider()
    provider.reconcile = lambda registry, budget: type(
        "Report",
        (),
        {
            "corpus_standing": (
                {
                    "corpus_id": "codex-history",
                    "sources": (
                        {
                            "source_id": "machine-uuid",
                            "members": ({"member_id": "member-1", "episode_count": 0, "source_standing": "malformed", "index_standing": "unavailable"},),
                        },
                    ),
                },
            ),
            "bytes_read": 9,
            "elapsed_ms": 1.0,
            "work_exhausted": False,
        },
    )()
    event_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(event_path))
    monkeypatch.setattr(mcp_server, "load_provider", lambda: provider)
    monkeypatch.setattr(mcp_server, "load_registry", lambda: registry)

    _run_in_lifespan(lambda: None)

    records = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    reconciliation = next(record for record in records if record["event"] == "reconcile.completed")
    assert reconciliation["source_standing"] == "malformed"
    assert reconciliation["episode_count"] == 0
    assert "body" not in json.dumps(reconciliation)
```

- [ ] **Step 4: Run MCP tests and observe missing instrumentation**

Run: `uv run python -m pytest tests/test_mcp_server.py -k 'emit_identifier or error_event or malformed_reconciliation' -v`

Expected: failures because `mcp_server.emit_event` and event calls do not exist.

- [ ] **Step 5: Add safe summarization helpers and lifespan events**

Import `emit_event` in `llm_memory/mcp_server.py`, then add:

```python
def _emit_failure(phase: str, code: str, exc: BaseException, **identifiers) -> None:
    emit_event(
        f"{phase}.failed",
        {"exception_class": type(exc).__name__, "diagnostic_code": code, **identifiers},
    )


def _emit_reconciliation(report) -> None:
    for corpus in report.corpus_standing:
        for source in corpus["sources"]:
            for member in source["members"]:
                emit_event(
                    "reconcile.completed",
                    {
                        "corpus_ids": [corpus["corpus_id"]],
                        "source_id": source["source_id"],
                        "member_id": member["member_id"],
                        "source_standing": member["source_standing"],
                        "index_standing": member["index_standing"],
                        "episode_count": member["episode_count"],
                        "bytes_read": report.bytes_read,
                        "duration_ms": report.elapsed_ms,
                        "work_exhausted": report.work_exhausted,
                    },
                )
```

Update `_lifespan` so it emits `server.starting` before provider loading, emits a fixed-code failure before re-raising setup errors, calls `_emit_reconciliation(report)`, emits `server.started` after runtime selection, and emits `server.stopped` in `finally`. Preserve the existing special case where a missing enrollment yields `{}` and leaves contract tools inactive; emit `server.started` with `outcome: "enrollment_missing"` for that branch.

- [ ] **Step 6: Instrument contract search/open without logging payloads**

Wrap `search_history` and `open_episode` in `try`/`except BaseException` blocks that re-raise after `_emit_failure`. After success emit only identifiers and outcomes:

```python
response = provider.search(registry, request, _budget())
emit_event(
    "search.completed",
    {
        "corpus_ids": list(corpus_ids),
        "returned_count": response.get("returned_count", 0),
        "episode_refs": [result["episode_ref"] for result in response.get("results", ())],
        "outcome": "completed",
    },
)
return response
```

```python
response = _open_episode(
    registry,
    episode_ref,
    active_corpus_ids,
    provider.resolve_supersession,
)
emit_event(
    "open.completed",
    {
        "corpus_ids": list(active_corpus_ids),
        "episode_ref": episode_ref,
        "standing": response.get("standing", "unknown"),
    },
)
return response
```

The failure branches use fixed codes `contract_search_failed`, `contract_open_failed`, or `server_startup_failed` and include only corpus/episode identifiers plus `exception_class`.

- [ ] **Step 7: Run focused and full tests**

Run: `uv run python -m pytest tests/test_mcp_server.py tests/test_reconcile.py tests/test_history_search.py tests/test_sqlite_history.py -v`

Expected: instrumentation tests pass; existing lifespan ownership and public response tests remain green.

Run: `uv run python -m pytest -q`

Expected: complete suite passes with one existing skip.

- [ ] **Step 8: Commit MCP observability**

```bash
git add llm_memory/reconcile.py llm_memory/mcp_server.py tests/test_reconcile.py tests/test_mcp_server.py
git commit -m "feat: observe episodic MCP operations"
```

---

### Task 5: Enroll and Verify the Live Local Codex Source

**Files:**
- Create locally, do not commit: `config/sources.yaml`
- Observe only: `/home/tony/.codex/sessions/2026/07/22/rollout-2026-07-22T17-50-42-019f8af3-83db-7972-af11-1d6309ad3392.jsonl`
- Observe only: `/home/tony/.local/state/llm-memory/events.jsonl`

**Interfaces:**
- Consumes: `linux_machine_uuid()`, `load_registry()`, `ArangoProvider`, `reconcile_registry()`.
- Produces: one enabled `codex-history` enrollment with source ID `e8c598ae-711b-42b5-b963-eb35fc946d2b`.
- Success gate: source and member standing `available`, index standing `available`, episode count greater than zero, and identifier-only event evidence.

- [ ] **Step 1: Confirm live identity and source without printing conversation content**

Run:

```bash
uv run python - <<'PY'
from pathlib import Path
from llm_memory.machine_identity import linux_machine_uuid

source = Path('/home/tony/.codex/sessions/2026/07/22/rollout-2026-07-22T17-50-42-019f8af3-83db-7972-af11-1d6309ad3392.jsonl')
print('machine_uuid', linux_machine_uuid())
print('source_exists', source.is_file())
print('source_bytes', source.stat().st_size)
PY
```

Expected: UUID is `e8c598ae-711b-42b5-b963-eb35fc946d2b`, `source_exists True`, and `source_bytes` is positive.

- [ ] **Step 2: Create the ignored enrollment with `apply_patch`**

Create `config/sources.yaml` exactly as:

```yaml
contract_version: 1
sources:
  - corpus_id: codex-history
    source_id: e8c598ae-711b-42b5-b963-eb35fc946d2b
    adapter: codex_jsonl
    boundary_version: 1
    canonicalization_version: 1
    locator: /home/tony/.codex/sessions/2026/07/22/rollout-2026-07-22T17-50-42-019f8af3-83db-7972-af11-1d6309ad3392.jsonl
    enabled: true
    full_validation_max_age_seconds: 86400
```

Run: `git check-ignore -v config/sources.yaml && git status --short`

Expected: `.gitignore` owns the ignore rule and the file is absent from git status.

- [ ] **Step 3: Validate enrollment identity before database work**

Run:

```bash
uv run python - <<'PY'
from llm_memory.enrollment import load_registry
from llm_memory.machine_identity import linux_machine_uuid

registry = load_registry()
assert len(registry.sources) == 1
source = registry.sources[0]
assert source.corpus_id == 'codex-history'
assert source.source_id == linux_machine_uuid()
assert source.adapter == 'codex_jsonl'
assert source.enabled is True
print('enrollment_valid', source.corpus_id, source.source_id, source.adapter)
PY
```

Expected: one `enrollment_valid` line with identifiers only.

- [ ] **Step 4: Reconcile through the selected Arango provider and enforce the hard gate**

Run:

```bash
uv run python - <<'PY'
import asyncio
from llm_memory import mcp_server

async def reconcile_through_lifespan():
    async with mcp_server.mcp._mcp_server.lifespan(
        mcp_server.mcp._mcp_server
    ) as context:
        return context['startup_reconciliation']

report = asyncio.run(reconcile_through_lifespan())
source = report.corpus_standing[0]['sources'][0]
member = source['members'][0]
assert source['source_set_standing'] == 'available', source
assert member['source_standing'] == 'available', member
assert member['index_standing'] == 'available', member
assert member['episode_count'] > 0, member
print('reconcile_available', source['source_id'], member['member_id'], member['episode_count'])
PY
```

Expected: `reconcile_available` with the machine UUID, a member identifier, and a positive episode count.

- [ ] **Step 5: Verify controlled malformed-source evidence without touching the live enrollment**

Run only the automated malformed-source/event tests; do not repoint the live config:

```bash
uv run python -m pytest tests/test_adapters.py -k clean_lookalike_without_conversation -v
uv run python -m pytest tests/test_observability.py tests/test_mcp_server.py -k 'failure or error_event' -v
```

Expected: all selected tests pass.

- [ ] **Step 6: Inspect the persistent event envelope, never its source content**

Run:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

path = Path.home() / '.local/state/llm-memory/events.jsonl'
records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
for record in records[-20:]:
    forbidden = {'query', 'snippet', 'body', 'response', 'exception_message', 'password'}
    assert not forbidden.intersection(record), record
assert any(record['event'] == 'reconcile.completed' and record.get('episode_count', 0) > 0 for record in records)
print('event_log_valid', len(records), oct(path.stat().st_mode & 0o777))
PY
```

Expected: `event_log_valid`, a positive count, and mode `0o600`.

- [ ] **Step 7: Run the complete regression suite**

Run: `uv run python -m pytest -q`

Expected: all tests pass with one existing skip.

No commit: both runtime files are intentionally local and ignored.

---

### Task 6: Register the Project-Scoped MCP Server and Perform the Restart Trial

**Files:**
- Create locally, do not commit: `/home/tony/projects/qhaway/.codex/config.toml`
- Preserve: `/home/tony/projects/qhaway/pyproject.toml` user modification.

**Interfaces:**
- Consumes: Codex project-scoped `[mcp_servers.llm-memory]` configuration.
- Produces after restart: MCP tools `search_history` and `open_episode` available in the trusted `qhaway` project.
- Rollback: remove only `.codex/config.toml` if it was created solely for this trial, or remove only its `[mcp_servers.llm-memory]` table if the file contains other user settings; then restart Codex.

- [ ] **Step 1: Recheck qhaway's local state before writing**

Run:

```bash
git -C /home/tony/projects/qhaway status --short
test ! -e /home/tony/projects/qhaway/.codex/config.toml
```

Expected: the pre-existing `pyproject.toml` modification remains visible and no project Codex config exists. If the config now exists, inspect and merge surgically rather than replacing it.

- [ ] **Step 2: Keep the machine-local Codex config out of repository status**

Use `apply_patch` to append this exact line to
`/home/tony/projects/qhaway/.git/info/exclude` if it is not already present:

```gitignore
.codex/config.toml
```

Run: `git -C /home/tony/projects/qhaway check-ignore -v .codex/config.toml`

Expected: `.git/info/exclude` is the owning ignore rule.

- [ ] **Step 3: Create the project-scoped configuration with `apply_patch`**

Create `/home/tony/projects/qhaway/.codex/config.toml` as:

```toml
[mcp_servers.llm-memory]
enabled = true
required = true
command = "uv"
args = ["run", "--directory", "/home/tony/projects/llm-memory", "python", "-m", "llm_memory.mcp_server"]
startup_timeout_sec = 30.0
tool_timeout_sec = 60.0
enabled_tools = ["search_history", "open_episode"]
```

Do not include DB credentials or source content. The server reads the gitignored local configuration from `llm-memory/config/`.

- [ ] **Step 4: Validate configuration and server initialization before restart**

Run:

```bash
(cd /home/tony/projects/qhaway && codex mcp list)
timeout 5s uv run --directory /home/tony/projects/llm-memory python -m llm_memory.mcp_server
```

Expected: `codex mcp list` recognizes `llm-memory` when evaluated from the trusted qhaway project. The stdio server remains alive until `timeout` ends it; it must not exit early with configuration, provider, or reconciliation errors. Exit 124 from `timeout` is the expected success signal.

- [ ] **Step 5: Ask Tony to restart Codex in `/home/tony/projects/qhaway`**

This is the only human coordination gate. Do not claim MCP availability before the restarted instance reports it.

- [ ] **Step 6: In the restarted instance, verify MCP connection and search**

Use the MCP status view to confirm `llm-memory` is connected. Then call:

```text
search_history(
  query="Will you permit me to wander with you?",
  corpus_ids=["codex-history"],
  limit=10,
)
```

Required assertions:

- response reports available index/source standing;
- `returned_count >= 1`;
- at least one result has an episode reference whose decoded source ID is `e8c598ae-711b-42b5-b963-eb35fc946d2b`.

- [ ] **Step 7: Open the matched authoritative episode**

Call `open_episode` with the exact `episode_ref` field from the matching
`search_history` result and `active_corpus_ids=["codex-history"]`.

Required assertions:

- `standing == "available"`;
- provenance `source_id == "e8c598ae-711b-42b5-b963-eb35fc946d2b"`;
- the full episode contains the invitation to wander and its paired response;
- the episode was opened from the authoritative source, not reconstructed from the search snippet.

- [ ] **Step 8: Verify post-restart event evidence**

Inspect only event names and identifier fields from the last records. Require `server.started`, `search.completed`, and `open.completed`; require the searched/opened episode reference; assert no query, snippet, body, or response fields exist.

- [ ] **Step 9: Record the trial verdict**

Append a short findings document under `docs/findings-2026-07-23-local-codex-dogfood.md` only after the live result exists. Record mechanical standing, counts, identifiers, errors, and qualitative usefulness. Do not copy the conversation into the finding; the episode reference is sufficient.

Commit the finding separately:

```bash
git add docs/findings-2026-07-23-local-codex-dogfood.md
git commit -m "docs: record local Codex dogfood result"
```

If the trial fails, record the exact failing boundary and standing rather than weakening the success criteria.
