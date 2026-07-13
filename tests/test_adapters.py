import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from llm_memory.adapters import ScanCursor, SourceMember, get_adapter
from llm_memory.contract import ContractError, FreshnessStanding, SourceStanding
from llm_memory.enrollment import SourceEnrollment


def enrollment(adapter, locator, *, source_id="declared-stream"):
    return SourceEnrollment(
        corpus_id="portable-history",
        source_id=source_id,
        adapter=adapter,
        boundary_version=2,
        canonicalization_version=3,
        locator=Path(locator),
        enabled=True,
        full_validation_max_age_seconds=3600,
    )


def write_jsonl(path, records, *, final_newline=True):
    data = b"\n".join(
        json.dumps(record, ensure_ascii=False).encode("utf-8") for record in records
    )
    if final_newline:
        data += b"\n"
    path.write_bytes(data)
    return data


def scan(path, adapter_name, *, source_id="declared-stream"):
    declared = enrollment(adapter_name, path, source_id=source_id)
    adapter = get_adapter(adapter_name)
    member = adapter.members(declared)[0]
    return adapter.scan(declared, member)


def test_adapter_registry_is_explicit_and_metadata_is_immutable(tmp_path):
    adapter = get_adapter("taste_open_jsonl")

    assert adapter.name == "taste_open_jsonl"
    assert adapter.implementation_version
    with pytest.raises(FrozenInstanceError):
        adapter.implementation_version = "changed"
    with pytest.raises(ContractError, match="unsupported adapter: mystery"):
        get_adapter("mystery")


def test_taste_open_uses_declared_stream_and_native_cycle(tmp_path):
    path = tmp_path / "taste.jsonl"
    data = write_jsonl(
        path,
        [
            {
                "cycle": 457,
                "timestamp": "2026-03-31T05:59:03Z",
                "model": "claude-haiku",
                "experiment_label": "taste_open",
                "user_message": "what did you mean?",
                "raw_output": {"response": "recognition enhancement"},
                "state": {
                    "observation": "drowning wall",
                    "_activity_log": [{"tool": "search_memory"}],
                },
            }
        ],
    )

    result = scan(path, "taste_open_jsonl", source_id="taste-stream")
    episode = result.episodes[0]

    assert result.member.member_id == "taste-stream"
    assert result.observed_end == result.complete_end == len(data)
    assert result.source_standing is SourceStanding.AVAILABLE
    assert result.freshness is FreshnessStanding.CURRENT
    assert episode.identity.reference.native_session_id == "taste-stream"
    assert episode.identity.reference.event_token == "457"
    assert episode.native_event_id == "457"
    assert episode.source_position == {"start": 0, "end": len(data)}
    assert episode.body.response == "recognition enhancement"
    assert episode.body.activity_log == [{"tool": "search_memory"}]
    assert "drowning wall" in episode.state_text
    assert "search_memory" not in episode.state_text


def test_gateway_uses_session_local_sequence_and_keeps_prompt_only_provenance(tmp_path):
    path = tmp_path / "gateway.jsonl"
    messages = [
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
        {"role": "user", "content": [{"type": "text", "text": "latest prompt"}]},
    ]
    write_jsonl(
        path,
        [
            {"type": "anomaly", "session_id": "session-a"},
            {
                "type": "request_metrics",
                "session_id": "session-a",
                "timestamp": "2026-06-18T20:00:00Z",
                "model": "claude-opus",
                "messages_full": messages,
            },
            {
                "type": "request_metrics",
                "session_id": "session-b",
                "messages_full": [{"role": "user", "content": "other"}],
                "response_text": "other response",
            },
            {
                "type": "request_metrics",
                "session_id": "session-a",
                "messages_full": [{"role": "user", "content": "next"}],
                "response_text": "next response",
            },
        ],
    )

    result = scan(path, "gateway_jsonl", source_id="gateway-stream")
    first, other, second = result.episodes

    assert first.identity.reference.native_session_id == "session-a"
    assert first.identity.reference.event_token == "0"
    assert other.identity.reference.native_session_id == "session-b"
    assert other.identity.reference.event_token == "0"
    assert second.identity.reference.event_token == "1"
    assert first.native_event_id is None
    assert first.body.user_message == "latest prompt"
    assert first.body.response == ""
    assert first.body.adapter_fields == {"messages_full": messages}


@pytest.mark.parametrize("messages_full", [{}, ""])
def test_gateway_rejects_falsey_non_list_provenance(tmp_path, messages_full):
    path = tmp_path / "gateway.jsonl"
    write_jsonl(
        path,
        [
            {
                "type": "request_metrics",
                "session_id": "session-a",
                "messages_full": messages_full,
            }
        ],
    )

    result = scan(path, "gateway_jsonl")

    assert result.episodes == ()
    assert result.source_standing is SourceStanding.MALFORMED
    assert result.error_position == 0


def test_claude_reuses_user_prose_for_repeated_assistants_and_skips_non_episodes(tmp_path):
    path = tmp_path / "session.jsonl"
    write_jsonl(
        path,
        [
            {"type": "progress", "data": {"status": "starting"}},
            {
                "type": "user",
                "sessionId": "native/session?yes",
                "uuid": "user-1",
                "timestamp": "2026-07-12T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "question"}]},
            },
            {
                "type": "assistant",
                "sessionId": "native/session?yes",
                "uuid": "assistant/one",
                "timestamp": "2026-07-12T10:00:01Z",
                "message": {
                    "model": "claude-test",
                    "content": [{"type": "tool_use", "name": "Read"}],
                },
            },
            {
                "type": "assistant",
                "sessionId": "native/session?yes",
                "uuid": "assistant/two",
                "timestamp": "2026-07-12T10:00:02Z",
                "message": {"model": "claude-test", "content": "first prose"},
            },
            {
                "type": "assistant",
                "sessionId": "native/session?yes",
                "uuid": "assistant/three",
                "timestamp": "2026-07-12T10:00:03Z",
                "message": {
                    "model": "claude-test",
                    "content": [{"type": "text", "text": "second prose"}],
                },
            },
            {
                "type": "user",
                "sessionId": "native/session?yes",
                "uuid": "user-unanswered",
                "message": {"content": "unanswered"},
            },
        ],
    )

    result = scan(path, "claude_code_jsonl", source_id="claude-project")

    assert result.member.member_id.startswith("member-")
    assert len(result.episodes) == 2
    assert [episode.body.user_message for episode in result.episodes] == [
        "question",
        "question",
    ]
    assert [episode.body.response for episode in result.episodes] == [
        "first prose",
        "second prose",
    ]
    assert [episode.identity.reference.event_token for episode in result.episodes] == [
        "assistant/two",
        "assistant/three",
    ]
    assert all(
        episode.identity.reference.native_session_id == "native/session?yes"
        for episode in result.episodes
    )


def test_incomplete_final_line_is_not_emitted_and_reports_complete_boundary(tmp_path):
    path = tmp_path / "partial.jsonl"
    first = json.dumps(
        {"cycle": 1, "user_message": "complete", "response_text": "answer"}
    ).encode()
    partial = json.dumps(
        {"cycle": 2, "user_message": "partial", "response_text": "ignored"}
    ).encode()
    path.write_bytes(first + b"\n" + partial)

    result = scan(path, "taste_open_jsonl")

    assert len(result.episodes) == 1
    assert result.observed_end == len(first) + 1 + len(partial)
    assert result.complete_end == len(first) + 1
    assert result.source_standing is SourceStanding.AVAILABLE
    assert result.freshness is FreshnessStanding.INCOMPLETE


def test_scan_does_not_cross_the_size_captured_after_open(tmp_path, monkeypatch):
    path = tmp_path / "growing.jsonl"
    initial = write_jsonl(
        path, [{"cycle": 1, "user_message": "first", "response_text": "one"}]
    )
    appended = json.dumps(
        {"cycle": 2, "user_message": "second", "response_text": "two"}
    ).encode() + b"\n"
    original_open = Path.open

    class AppendBeforeFirstRead:
        def __init__(self, source):
            self.source = source
            self.appended = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.source.__exit__(*args)

        def fileno(self):
            return self.source.fileno()

        def tell(self):
            return self.source.tell()

        def readline(self, *args):
            if not self.appended:
                self.appended = True
                with original_open(path, "ab") as destination:
                    destination.write(appended)
            return self.source.readline(*args)

    def growing_open(self, *args, **kwargs):
        source = original_open(self, *args, **kwargs)
        if self == path and args and args[0] == "rb":
            return AppendBeforeFirstRead(source)
        return source

    monkeypatch.setattr(Path, "open", growing_open)

    result = scan(path, "taste_open_jsonl")

    assert result.observed_end == result.complete_end == len(initial)
    assert len(result.episodes) == 1
    assert result.episodes[0].body.user_message == "first"


@pytest.mark.parametrize("bad_line", [b"{not json}\n", b'"\xff"\n'])
def test_malformed_complete_line_reports_its_byte_position(tmp_path, bad_line):
    path = tmp_path / "malformed.jsonl"
    valid = json.dumps(
        {"cycle": 1, "user_message": "complete", "response_text": "answer"}
    ).encode() + b"\n"
    path.write_bytes(valid + bad_line)

    result = scan(path, "taste_open_jsonl")

    assert len(result.episodes) == 1
    assert result.error_position == len(valid)
    assert result.source_standing is SourceStanding.MALFORMED
    assert result.freshness is FreshnessStanding.UNKNOWN


def test_relocating_identical_source_does_not_change_episode_references(tmp_path):
    record = {
        "type": "request_metrics",
        "session_id": "session with spaces",
        "messages_full": [{"role": "user", "content": "portable"}],
        "response_text": "stable",
    }
    first = tmp_path / "first" / "gateway.jsonl"
    second = tmp_path / "second" / "renamed.jsonl"
    first.parent.mkdir()
    second.parent.mkdir()
    write_jsonl(first, [record])
    second.write_bytes(first.read_bytes())

    first_scan = scan(first, "gateway_jsonl", source_id="stable-source")
    second_scan = scan(second, "gateway_jsonl", source_id="stable-source")

    assert first_scan.episodes[0].identity.episode_ref == second_scan.episodes[0].identity.episode_ref
    assert " " not in first_scan.episodes[0].identity.episode_ref


def test_implementation_version_is_metadata_not_an_identity_input(tmp_path):
    path = tmp_path / "taste.jsonl"
    write_jsonl(path, [{"cycle": 9, "user_message": "q", "response_text": "a"}])
    adapter = get_adapter("taste_open_jsonl")
    declared = enrollment("taste_open_jsonl", path)
    member = adapter.members(declared)[0]

    before = adapter.scan(declared, member).episodes[0].identity.episode_ref
    released_adapter = replace(
        adapter, implementation_version="release-only-change"
    )
    after = released_adapter.scan(declared, member).episodes[0].identity.episode_ref

    assert before == after


def test_claude_member_without_any_native_session_is_visibly_malformed(tmp_path):
    source_set = tmp_path / "sessions"
    source_set.mkdir()
    path = source_set / "empty.jsonl"
    path.write_bytes(b"")
    declared = enrollment("claude_code_jsonl", source_set)
    adapter = get_adapter("claude_code_jsonl")
    member = adapter.members(declared)[0]

    result = adapter.scan(declared, member)

    assert member.member_id.startswith("member-")
    assert result.source_standing is SourceStanding.MALFORMED
    assert result.freshness is FreshnessStanding.UNKNOWN
    assert result.error_position == 0
    assert result.episodes == ()


def test_claude_native_session_may_begin_with_unresolved_prefix(tmp_path):
    path = tmp_path / "session.jsonl"
    write_jsonl(
        path,
        [
            {
                "type": "assistant",
                "sessionId": "unresolved-native",
                "uuid": "answer-1",
                "message": {"content": "answer"},
            }
        ],
    )

    result = scan(path, "claude_code_jsonl")

    assert result.member.member_id.startswith("member-")
    assert result.source_standing is SourceStanding.AVAILABLE
    assert len(result.episodes) == 1
    assert (
        result.episodes[0].identity.reference.native_session_id
        == "unresolved-native"
    )


def test_claude_directory_members_are_sorted_and_unresolved_ids_are_operational(tmp_path):
    source_set = tmp_path / "sessions"
    source_set.mkdir()
    good = source_set / "b.jsonl"
    bad = source_set / "a.jsonl"
    bad.write_bytes(b"{broken}\n")
    write_jsonl(
        good,
        [
            {
                "type": "assistant",
                "sessionId": "session-b",
                "uuid": "answer-b",
                "message": {"content": "answer"},
            }
        ],
    )
    declared = enrollment("claude_code_jsonl", source_set, source_id="claude-project")
    adapter = get_adapter("claude_code_jsonl")

    members = adapter.members(declared)

    assert [member.path.name for member in members] == ["a.jsonl", "b.jsonl"]
    assert all(member.member_id.startswith("member-") for member in members)
    malformed = adapter.scan(declared, members[0])
    assert malformed.source_standing is SourceStanding.MALFORMED
    assert malformed.error_position == 0
    assert malformed.episodes == ()
    assert all(
        member.member_id not in episode.identity.episode_ref
        for member in members
        for episode in adapter.scan(declared, member).episodes
        if member.member_id.startswith("member-")
    )


def test_source_member_is_frozen():
    member = SourceMember("member", Path("source.jsonl"))

    with pytest.raises(FrozenInstanceError):
        member.member_id = "changed"


def test_gateway_chunk_scan_preserves_sequence_state_and_matches_full_scan(tmp_path):
    path = tmp_path / "gateway-chunks.jsonl"
    data = write_jsonl(
        path,
        [
            {
                "type": "request_metrics",
                "session_id": "session-a",
                "messages_full": [{"role": "user", "content": "one"}],
                "response_text": "first",
            },
            {
                "type": "request_metrics",
                "session_id": "session-a",
                "messages_full": [{"role": "user", "content": "two"}],
                "response_text": "second",
            },
        ],
    )
    declared = enrollment("gateway_jsonl", path)
    adapter = get_adapter(declared.adapter)
    member = adapter.members(declared)[0]

    first = adapter.scan_chunk(declared, member, None, 1)
    second = adapter.scan_chunk(declared, member, first.next_cursor, 1)
    complete = adapter.scan(declared, member)

    assert first.bytes_read == first.next_cursor.byte_offset
    assert first.exhausted is True
    assert first.next_cursor.adapter_state == {"sequence_by_session": {"session-a": 1}}
    assert second.bytes_read == len(data) - first.bytes_read
    assert second.next_cursor.byte_offset == len(data)
    assert second.next_cursor.adapter_state == {"sequence_by_session": {"session-a": 2}}
    assert first.episodes + second.episodes == complete.episodes


def test_claude_chunk_scan_preserves_conversation_state(tmp_path):
    path = tmp_path / "claude-chunks.jsonl"
    records = [
        {
            "type": "user",
            "sessionId": "session-a",
            "timestamp": "2026-07-12T10:00:00Z",
            "message": {"content": "remember this"},
        },
        {
            "type": "assistant",
            "sessionId": "session-a",
            "uuid": "answer-a",
            "message": {"content": "I remember"},
        },
    ]
    data = write_jsonl(path, records)
    first_line_end = data.index(b"\n") + 1
    declared = enrollment("claude_code_jsonl", path)
    adapter = get_adapter(declared.adapter)
    member = adapter.members(declared)[0]

    first = adapter.scan_chunk(declared, member, ScanCursor(0, {}), first_line_end)
    second = adapter.scan_chunk(declared, member, first.next_cursor, 1)

    assert first.next_cursor.adapter_state == {
        "last_user": "remember this",
        "last_user_ts": "2026-07-12T10:00:00Z",
        "session_established": True,
    }
    assert first.episodes == ()
    assert second.episodes[0].body.user_message == "remember this"
    assert second.next_cursor.byte_offset == len(data)


def test_chunk_scan_declares_single_record_overshoot_and_stops_before_next(tmp_path):
    path = tmp_path / "oversized.jsonl"
    data = write_jsonl(
        path,
        [
            {"cycle": 1, "user_message": "x" * 200, "response_text": "one"},
            {"cycle": 2, "user_message": "next", "response_text": "two"},
        ],
    )
    first_line_end = data.index(b"\n") + 1
    declared = enrollment("taste_open_jsonl", path)
    adapter = get_adapter(declared.adapter)
    member = adapter.members(declared)[0]

    chunk = adapter.scan_chunk(declared, member, None, 10)

    assert chunk.bytes_read == first_line_end > 10
    assert chunk.next_cursor.byte_offset == first_line_end
    assert len(chunk.episodes) == 1
    assert chunk.exhausted is True
    assert chunk.complete_end == first_line_end


def test_blank_complete_line_advances_chunk_cursor(tmp_path):
    path = tmp_path / "blank.jsonl"
    record = json.dumps(
        {"cycle": 1, "user_message": "after blank", "response_text": "answer"}
    ).encode() + b"\n"
    path.write_bytes(b"\n" + record)
    declared = enrollment("taste_open_jsonl", path)
    adapter = get_adapter(declared.adapter)
    member = adapter.members(declared)[0]

    blank = adapter.scan_chunk(declared, member, None, 1)
    episode = adapter.scan_chunk(declared, member, blank.next_cursor, len(record))

    assert blank.bytes_read == 1
    assert blank.next_cursor.byte_offset == 1
    assert blank.exhausted is True
    assert len(episode.episodes) == 1


def test_claude_member_discovery_uses_relative_name_without_reading_source(tmp_path, monkeypatch):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = first_root / "session.jsonl"
    second = second_root / "session.jsonl"
    write_jsonl(first, [
        {
            "type": "assistant",
            "sessionId": "native-a",
            "uuid": "answer-a",
            "message": {"content": "answer"},
        }
    ])
    second.write_bytes(first.read_bytes())
    adapter = get_adapter("claude_code_jsonl")
    original_open = Path.open

    def forbidden_open(self, *args, **kwargs):
        if self in {first, second}:
            raise AssertionError("member discovery read source contents")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbidden_open)

    first_member = adapter.members(enrollment("claude_code_jsonl", first_root))[0]
    second_member = adapter.members(enrollment("claude_code_jsonl", second_root))[0]

    assert first_member.member_id == second_member.member_id
    assert first_member.member_id.startswith("member-")
