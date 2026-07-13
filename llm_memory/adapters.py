from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

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


def _identity(
    enrollment: SourceEnrollment,
    native_session_id: str,
    event_token: str,
    body: EpisodeBody,
) -> EpisodeIdentity:
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


def _scan_lines(member: SourceMember, handler: RecordHandler) -> MemberScan:
    try:
        source = member.path.open("rb")
    except FileNotFoundError:
        return MemberScan(
            member,
            (),
            0,
            0,
            SourceStanding.MISSING,
            FreshnessStanding.UNAVAILABLE,
        )
    except OSError:
        return MemberScan(
            member,
            (),
            0,
            0,
            SourceStanding.UNAVAILABLE,
            FreshnessStanding.UNAVAILABLE,
        )

    try:
        observed_end = os.fstat(source.fileno()).st_size
    except OSError:
        source.close()
        return MemberScan(
            member,
            (),
            0,
            0,
            SourceStanding.UNAVAILABLE,
            FreshnessStanding.UNAVAILABLE,
        )

    episodes: list[EpisodeRecord] = []
    complete_end = 0
    error_position = None
    incomplete = False
    with source:
        while source.tell() < observed_end:
            start = source.tell()
            line = source.readline(observed_end - start)
            if not line:
                incomplete = True
                break
            end = source.tell()
            if not line.endswith(b"\n"):
                incomplete = True
                break
            complete_end = end
            if not line.strip():
                continue
            try:
                decoded = json.loads(line.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise ValueError("JSONL record must be an object")
                episode = handler(decoded, start, end)
            except (ContractError, KeyError, TypeError, UnicodeDecodeError, ValueError):
                error_position = start
                break
            if episode is not None:
                episodes.append(episode)

    if error_position is not None:
        source_standing = SourceStanding.MALFORMED
        freshness = FreshnessStanding.UNKNOWN
    else:
        source_standing = SourceStanding.AVAILABLE
        freshness = (
            FreshnessStanding.INCOMPLETE
            if incomplete
            else FreshnessStanding.CURRENT
        )
    return MemberScan(
        member=member,
        episodes=tuple(episodes),
        observed_end=observed_end,
        complete_end=complete_end,
        source_standing=source_standing,
        freshness=freshness,
        error_position=error_position,
    )


def _file_member(enrollment: SourceEnrollment) -> tuple[SourceMember, ...]:
    return (SourceMember(enrollment.source_id, enrollment.locator),)


@dataclass(frozen=True)
class TasteOpenAdapter:
    name: str = "taste_open_jsonl"
    implementation_version: str = "1"

    def members(self, enrollment: SourceEnrollment) -> tuple[SourceMember, ...]:
        return _file_member(enrollment)

    def scan(
        self, enrollment: SourceEnrollment, member: SourceMember
    ) -> MemberScan:
        def handle(record: dict[str, Any], start: int, end: int) -> EpisodeRecord:
            cycle = str(record["cycle"])
            state = record.get("state") or {}
            if not isinstance(state, dict):
                raise ValueError("state must be an object")
            raw = record.get("raw_output")
            response = raw.get("response") if isinstance(raw, dict) else None
            if not response:
                response = record.get("response_text", "")
            activity_log = state.get("_activity_log", [])
            if not isinstance(activity_log, list):
                raise ValueError("activity log must be a list")
            state_text = flatten_state(
                {key: value for key, value in state.items() if key != "_activity_log"}
            )
            body = EpisodeBody(
                timestamp=record.get("timestamp") or "",
                model=record.get("model") or "",
                user_message=record.get("user_message") or "",
                response=response or "",
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

        return _scan_lines(member, handle)


@dataclass(frozen=True)
class GatewayAdapter:
    name: str = "gateway_jsonl"
    implementation_version: str = "1"

    def members(self, enrollment: SourceEnrollment) -> tuple[SourceMember, ...]:
        return _file_member(enrollment)

    def scan(
        self, enrollment: SourceEnrollment, member: SourceMember
    ) -> MemberScan:
        sequence_by_session: dict[str, int] = {}

        def handle(
            record: dict[str, Any], start: int, end: int
        ) -> EpisodeRecord | None:
            if record.get("type") != "request_metrics":
                return None
            session = record.get("session_id")
            if not isinstance(session, str) or not session:
                raise ValueError("request_metrics session_id is required")
            sequence = sequence_by_session.get(session, 0)
            sequence_by_session[session] = sequence + 1
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
                timestamp=record.get("timestamp") or "",
                model=record.get("model") or "",
                user_message=user_message,
                response=record.get("response_text") or "",
                state={},
                activity_log=[],
                adapter_fields={"messages_full": messages},
            )
            return _record(
                enrollment,
                native_session_id=session,
                event_token=str(sequence),
                body=body,
                native_event_id=None,
                start=start,
                end=end,
            )

        return _scan_lines(member, handle)


def _unresolved_member_id(relative_name: str) -> str:
    digest = hashlib.sha256(relative_name.encode("utf-8")).hexdigest()
    return f"unresolved-{digest}"


def _claude_member_id(path: Path, relative_name: str) -> str:
    try:
        with path.open("rb") as source:
            for line in source:
                if not line.endswith(b"\n"):
                    break
                try:
                    record = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(record, dict):
                    session = record.get("sessionId")
                    if isinstance(session, str) and session:
                        return session
    except OSError:
        pass
    return _unresolved_member_id(relative_name)


@dataclass(frozen=True)
class ClaudeCodeAdapter:
    name: str = "claude_code_jsonl"
    implementation_version: str = "1"

    def members(self, enrollment: SourceEnrollment) -> tuple[SourceMember, ...]:
        locator = enrollment.locator
        if locator.is_dir():
            paths = sorted(locator.glob("*.jsonl"))
            return tuple(
                SourceMember(
                    _claude_member_id(path, path.relative_to(locator).as_posix()),
                    path,
                )
                for path in paths
            )
        return (
            SourceMember(
                _claude_member_id(locator, locator.name),
                locator,
            ),
        )

    def scan(
        self, enrollment: SourceEnrollment, member: SourceMember
    ) -> MemberScan:
        last_user = ""
        session_established = False

        def handle(
            record: dict[str, Any], start: int, end: int
        ) -> EpisodeRecord | None:
            nonlocal last_user, session_established
            session = record.get("sessionId")
            if isinstance(session, str) and session:
                session_established = True
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
                return None
            response = turn_text(message.get("content"))
            if not response.strip():
                return None
            event_id = record.get("uuid")
            if not isinstance(event_id, str) or not event_id:
                raise ValueError("Claude assistant uuid is required")
            body = EpisodeBody(
                timestamp=record.get("timestamp") or "",
                model=message.get("model") or "",
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

        result = _scan_lines(member, handle)
        if (
            not session_established
            and result.source_standing is SourceStanding.AVAILABLE
        ):
            return MemberScan(
                member=result.member,
                episodes=(),
                observed_end=result.observed_end,
                complete_end=result.complete_end,
                source_standing=SourceStanding.MALFORMED,
                freshness=(
                    FreshnessStanding.INCOMPLETE
                    if result.freshness is FreshnessStanding.INCOMPLETE
                    else FreshnessStanding.UNKNOWN
                ),
                error_position=0,
            )
        return result


_ADAPTERS: dict[str, SourceAdapter] = {
    "taste_open_jsonl": TasteOpenAdapter(),
    "gateway_jsonl": GatewayAdapter(),
    "claude_code_jsonl": ClaudeCodeAdapter(),
}


def get_adapter(name: str) -> SourceAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError as exc:
        raise ContractError(f"unsupported adapter: {name}") from exc
