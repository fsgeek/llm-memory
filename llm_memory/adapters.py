from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from llm_memory.adapter_versions import supports_semantic_versions
from llm_memory.contract import (
    ContractError,
    EpisodeBody,
    EpisodeIdentity,
    FreshnessStanding,
    SourceStanding,
    build_identity,
)
from llm_memory.enrollment import SourceEnrollment
from llm_memory.schema import flatten_state


@dataclass(frozen=True)
class SourceMember:
    member_id: str
    path: Path


@dataclass(frozen=True)
class EpisodeRecord:
    identity: EpisodeIdentity
    body: EpisodeBody
    native_event_id: str | None
    source_position: dict
    state_text: str


@dataclass(frozen=True)
class ScanCursor:
    byte_offset: int
    adapter_state: dict


@dataclass(frozen=True)
class MemberChunk:
    member: SourceMember
    episodes: tuple[EpisodeRecord, ...]
    next_cursor: ScanCursor
    observed_end: int
    complete_end: int
    source_standing: SourceStanding
    freshness: FreshnessStanding
    bytes_read: int
    exhausted: bool
    error_position: int | None = None


@dataclass(frozen=True)
class MemberScan:
    member: SourceMember
    episodes: tuple[EpisodeRecord, ...]
    observed_end: int
    complete_end: int
    source_standing: SourceStanding
    freshness: FreshnessStanding
    error_position: int | None = None


class SourceAdapter(Protocol):
    name: str
    implementation_version: str

    def members(self, enrollment: SourceEnrollment) -> tuple[SourceMember, ...]: ...

    def scan(
        self, enrollment: SourceEnrollment, member: SourceMember
    ) -> MemberScan: ...

    def scan_chunk(
        self,
        enrollment: SourceEnrollment,
        member: SourceMember,
        cursor: ScanCursor | None,
        max_bytes: int,
    ) -> MemberChunk: ...


def turn_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _require_semantic_versions(enrollment: SourceEnrollment) -> None:
    if not supports_semantic_versions(
        enrollment.adapter,
        boundary_version=enrollment.boundary_version,
        canonicalization_version=enrollment.canonicalization_version,
    ):
        raise ContractError(
            "unsupported adapter semantic versions: "
            f"{enrollment.adapter} boundary={enrollment.boundary_version} "
            f"canonicalization={enrollment.canonicalization_version}"
        )


def _identity(
    enrollment: SourceEnrollment,
    native_session_id: str,
    event_token: str,
    body: EpisodeBody,
) -> EpisodeIdentity:
    _require_semantic_versions(enrollment)
    return build_identity(
        corpus_id=enrollment.corpus_id,
        source_id=enrollment.source_id,
        native_session_id=native_session_id,
        event_token=event_token,
        canonicalization_version=enrollment.canonicalization_version,
        boundary_version=enrollment.boundary_version,
        body=body,
    )


def _record(
    enrollment: SourceEnrollment,
    *,
    native_session_id: str,
    event_token: str,
    body: EpisodeBody,
    native_event_id: str | None,
    start: int,
    end: int,
    state_text: str = "",
) -> EpisodeRecord:
    return EpisodeRecord(
        identity=_identity(enrollment, native_session_id, event_token, body),
        body=body,
        native_event_id=native_event_id,
        source_position={"start": start, "end": end},
        state_text=state_text,
    )


RecordHandler = Callable[[dict[str, Any], int, int], EpisodeRecord | None]


def _scan_lines_chunk(
    member: SourceMember,
    cursor: ScanCursor | None,
    max_bytes: int,
    handler: RecordHandler,
    adapter_state: dict,
) -> MemberChunk:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    start_offset = cursor.byte_offset if cursor is not None else 0
    try:
        source = member.path.open("rb")
    except FileNotFoundError:
        return MemberChunk(
            member,
            (),
            ScanCursor(start_offset, adapter_state),
            0,
            0,
            SourceStanding.MISSING,
            FreshnessStanding.UNAVAILABLE,
            0,
            False,
        )
    except OSError:
        return MemberChunk(
            member,
            (),
            ScanCursor(start_offset, adapter_state),
            0,
            0,
            SourceStanding.UNAVAILABLE,
            FreshnessStanding.UNAVAILABLE,
            0,
            False,
        )

    try:
        observed_end = os.fstat(source.fileno()).st_size
    except OSError:
        source.close()
        return MemberChunk(
            member,
            (),
            ScanCursor(start_offset, adapter_state),
            0,
            0,
            SourceStanding.UNAVAILABLE,
            FreshnessStanding.UNAVAILABLE,
            0,
            False,
        )

    episodes: list[EpisodeRecord] = []
    complete_end = min(start_offset, observed_end)
    next_offset = complete_end
    bytes_read = 0
    error_position = None
    incomplete = False
    exhausted = False
    with source:
        if complete_end:
            source.seek(complete_end)
        while source.tell() < observed_end:
            if bytes_read >= max_bytes:
                exhausted = True
                break
            start = source.tell()
            line = source.readline(observed_end - start)
            bytes_read += len(line)
            if not line:
                incomplete = True
                break
            end = source.tell()
            if not line.endswith(b"\n"):
                incomplete = True
                break
            if not line.strip():
                complete_end = end
                next_offset = end
                continue
            try:
                decoded = json.loads(line.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise ValueError("JSONL record must be an object")
                episode = handler(decoded, start, end)
            except (
                ContractError,
                KeyError,
                RecursionError,
                TypeError,
                UnicodeDecodeError,
                ValueError,
            ):
                error_position = start
                break
            if episode is not None:
                episodes.append(episode)
            complete_end = end
            next_offset = end

    if error_position is not None:
        source_standing = SourceStanding.MALFORMED
        freshness = FreshnessStanding.UNKNOWN
    else:
        source_standing = SourceStanding.AVAILABLE
        freshness = (
            FreshnessStanding.INCOMPLETE
            if incomplete or exhausted
            else FreshnessStanding.CURRENT
        )
    return MemberChunk(
        member=member,
        episodes=tuple(episodes),
        next_cursor=ScanCursor(next_offset, adapter_state),
        observed_end=observed_end,
        complete_end=complete_end,
        source_standing=source_standing,
        freshness=freshness,
        bytes_read=bytes_read,
        exhausted=exhausted,
        error_position=error_position,
    )


def _member_scan(chunk: MemberChunk) -> MemberScan:
    return MemberScan(
        member=chunk.member,
        episodes=chunk.episodes,
        observed_end=chunk.observed_end,
        complete_end=chunk.complete_end,
        source_standing=chunk.source_standing,
        freshness=chunk.freshness,
        error_position=chunk.error_position,
    )


def _file_member(enrollment: SourceEnrollment) -> tuple[SourceMember, ...]:
    return (SourceMember(enrollment.source_id, enrollment.locator),)


def _optional_text(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


@dataclass(frozen=True)
class TasteOpenAdapter:
    name: str = "taste_open_jsonl"
    implementation_version: str = "2"

    def members(self, enrollment: SourceEnrollment) -> tuple[SourceMember, ...]:
        return _file_member(enrollment)

    def scan(
        self, enrollment: SourceEnrollment, member: SourceMember
    ) -> MemberScan:
        return _member_scan(self.scan_chunk(enrollment, member, None, 2**63 - 1))

    def scan_chunk(
        self,
        enrollment: SourceEnrollment,
        member: SourceMember,
        cursor: ScanCursor | None,
        max_bytes: int,
    ) -> MemberChunk:
        _require_semantic_versions(enrollment)

        def handle(record: dict[str, Any], start: int, end: int) -> EpisodeRecord:
            native_cycle = record["cycle"]
            if isinstance(native_cycle, bool) or not isinstance(
                native_cycle, (int, str)
            ):
                raise ValueError("cycle must be an integer or string")
            cycle = str(native_cycle)
            state = record.get("state")
            if state is None:
                state = {}
            if not isinstance(state, dict):
                raise ValueError("state must be an object")
            raw = record.get("raw_output")
            response = raw.get("response") if isinstance(raw, dict) else None
            if response is None or response == "":
                response = _optional_text(record, "response_text")
            elif not isinstance(response, str):
                raise ValueError("raw_output.response must be a string")
            activity_log = state.get("_activity_log", [])
            if not isinstance(activity_log, list):
                raise ValueError("activity log must be a list")
            state_text = flatten_state(
                {key: value for key, value in state.items() if key != "_activity_log"}
            )
            body = EpisodeBody(
                timestamp=_optional_text(record, "timestamp"),
                model=_optional_text(record, "model"),
                user_message=_optional_text(record, "user_message"),
                response=response,
                state=state,
                activity_log=activity_log,
                adapter_fields={
                    "experiment_label": record.get("experiment_label")
                },
            )
            return _record(
                enrollment,
                native_session_id=enrollment.source_id,
                event_token=cycle,
                body=body,
                native_event_id=cycle,
                start=start,
                end=end,
                state_text=state_text,
            )

        return _scan_lines_chunk(member, cursor, max_bytes, handle, {})


@dataclass(frozen=True)
class GatewayAdapter:
    name: str = "gateway_jsonl"
    implementation_version: str = "2"

    def members(self, enrollment: SourceEnrollment) -> tuple[SourceMember, ...]:
        return _file_member(enrollment)

    def scan(
        self, enrollment: SourceEnrollment, member: SourceMember
    ) -> MemberScan:
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
        sequence_by_session = dict(state.get("sequence_by_session", {}))

        def handle(
            record: dict[str, Any], start: int, end: int
        ) -> EpisodeRecord | None:
            if record.get("type") != "request_metrics":
                return None
            session = record.get("session_id")
            if not isinstance(session, str) or not session:
                raise ValueError("request_metrics session_id is required")
            sequence = sequence_by_session.get(session, 0)
            raw_messages = record.get("messages_full")
            messages = [] if raw_messages is None else raw_messages
            if not isinstance(messages, list):
                raise ValueError("messages_full must be a list")
            user_message = ""
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "user":
                    user_message = turn_text(message.get("content"))
                    break
            body = EpisodeBody(
                timestamp=_optional_text(record, "timestamp"),
                model=_optional_text(record, "model"),
                user_message=user_message,
                response=_optional_text(record, "response_text"),
                state={},
                activity_log=[],
                adapter_fields={"messages_full": messages},
            )
            episode = _record(
                enrollment,
                native_session_id=session,
                event_token=str(sequence),
                body=body,
                native_event_id=None,
                start=start,
                end=end,
            )
            sequence_by_session[session] = sequence + 1
            return episode

        state["sequence_by_session"] = sequence_by_session
        return _scan_lines_chunk(member, cursor, max_bytes, handle, state)


def _operational_member_id(relative_name: str) -> str:
    digest = hashlib.sha256(relative_name.encode("utf-8")).hexdigest()
    return f"member-{digest}"


@dataclass(frozen=True)
class ClaudeCodeAdapter:
    name: str = "claude_code_jsonl"
    implementation_version: str = "2"

    def members(self, enrollment: SourceEnrollment) -> tuple[SourceMember, ...]:
        locator = enrollment.locator
        if not locator.exists():
            return ()
        if locator.is_dir():
            paths = sorted(locator.glob("*.jsonl"))
            return tuple(
                SourceMember(
                    _operational_member_id(path.relative_to(locator).as_posix()),
                    path,
                )
                for path in paths
            )
        return (
            SourceMember(
                _operational_member_id(locator.name),
                locator,
            ),
        )

    def scan(
        self, enrollment: SourceEnrollment, member: SourceMember
    ) -> MemberScan:
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
        last_user = state.get("last_user", "")
        last_user_ts = state.get("last_user_ts", "")
        session_established = state.get("session_established", False)
        state.update(
            {
                "last_user": last_user,
                "last_user_ts": last_user_ts,
                "session_established": session_established,
            }
        )

        def handle(
            record: dict[str, Any], start: int, end: int
        ) -> EpisodeRecord | None:
            nonlocal last_user, last_user_ts, session_established
            session = record.get("sessionId")
            if isinstance(session, str) and session:
                session_established = True
                state["session_established"] = True
            message = record.get("message")
            if not isinstance(message, dict):
                return None
            record_type = record.get("type")
            if record_type not in {"user", "assistant"}:
                return None
            if not isinstance(session, str) or not session:
                raise ValueError("Claude sessionId is required")
            if record_type == "user":
                prose = turn_text(message.get("content"))
                if prose.strip():
                    last_user = prose
                    last_user_ts = record.get("timestamp") or ""
                    state["last_user"] = last_user
                    state["last_user_ts"] = last_user_ts
                return None
            response = turn_text(message.get("content"))
            if not response.strip():
                return None
            event_id = record.get("uuid")
            if not isinstance(event_id, str) or not event_id:
                raise ValueError("Claude assistant uuid is required")
            body = EpisodeBody(
                timestamp=_optional_text(record, "timestamp"),
                model=_optional_text(message, "model"),
                user_message=last_user,
                response=response,
                state={},
                activity_log=[],
                adapter_fields={},
            )
            return _record(
                enrollment,
                native_session_id=session,
                event_token=event_id,
                body=body,
                native_event_id=event_id,
                start=start,
                end=end,
            )

        result = _scan_lines_chunk(member, cursor, max_bytes, handle, state)
        if (
            not session_established
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
                freshness=(
                    FreshnessStanding.INCOMPLETE
                    if result.freshness is FreshnessStanding.INCOMPLETE
                    else FreshnessStanding.UNKNOWN
                ),
                bytes_read=result.bytes_read,
                exhausted=result.exhausted,
                error_position=0,
            )
        return result


@dataclass(frozen=True)
class CodexAdapter:
    name: str = "codex_jsonl"
    implementation_version: str = "1"

    def members(self, enrollment: SourceEnrollment) -> tuple[SourceMember, ...]:
        return _file_member(enrollment)

    def scan(
        self, enrollment: SourceEnrollment, member: SourceMember
    ) -> MemberScan:
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

        def handle(
            record: dict[str, Any], start: int, end: int
        ) -> EpisodeRecord | None:
            nonlocal native_session_id, latest_user, latest_user_ts
            nonlocal recognized_conversation
            record_type = record.get("type")
            if record_type not in {"session_meta", "event_msg"}:
                return None
            payload = record.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("Codex conversational payload must be an object")
            if record_type == "session_meta":
                session = payload.get("session_id")
                if not isinstance(session, str) or not session:
                    raise ValueError("Codex session_meta session_id is required")
                if session != native_session_id:
                    latest_user = ""
                    latest_user_ts = ""
                    state["latest_user"] = ""
                    state["latest_user_ts"] = ""
                native_session_id = session
                state["native_session_id"] = session
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
            if event_type == "user_message":
                user_timestamp = _optional_text(record, "timestamp")
                recognized_conversation = True
                state["recognized_conversation"] = True
                latest_user = message
                latest_user_ts = user_timestamp
                state["latest_user"] = latest_user
                state["latest_user_ts"] = latest_user_ts
                return None
            if not latest_user.strip():
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


_ADAPTERS: dict[str, SourceAdapter] = {
    "taste_open_jsonl": TasteOpenAdapter(),
    "gateway_jsonl": GatewayAdapter(),
    "claude_code_jsonl": ClaudeCodeAdapter(),
    "codex_jsonl": CodexAdapter(),
}


def get_adapter(name: str) -> SourceAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError as exc:
        raise ContractError(f"unsupported adapter: {name}") from exc
