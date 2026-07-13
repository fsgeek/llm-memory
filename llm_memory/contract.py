from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable
from urllib.parse import urlsplit


CONTRACT_VERSION = 1
STRATEGY = "lexical_bm25_text_en_v1"
MAX_LIMIT = 100
_CORPUS_ID_PATTERN = re.compile(r"[A-Za-z0-9._~-]+")


class ContractError(ValueError):
    """Raised when a value does not satisfy the episodic contract."""


class SourceStanding(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MISSING = "missing"
    UNKNOWN = "unknown"
    UNSUPPORTED_ADAPTER = "unsupported_adapter"
    MALFORMED = "malformed"


class IndexStanding(StrEnum):
    AVAILABLE = "available"
    REBUILDING = "rebuilding"
    UNAVAILABLE = "unavailable"


class FreshnessStanding(StrEnum):
    CURRENT = "current"
    TAIL_VALIDATED = "tail_validated"
    STALE = "stale"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class TotalStanding(StrEnum):
    EXACT = "exact"
    ESTIMATED = "estimated"
    LOWER_BOUND = "lower_bound"
    UNKNOWN = "unknown"


class OpenStanding(StrEnum):
    AVAILABLE = "available"
    SOURCE_UNAVAILABLE = "source_unavailable"
    MISSING = "missing"
    CONTENT_MISMATCH = "content_mismatch"
    UNSUPPORTED_ADAPTER = "unsupported_adapter"
    MALFORMED_SOURCE = "malformed_source"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class EpisodeBody:
    timestamp: str
    model: str
    user_message: str
    response: str
    state: dict[str, Any]
    activity_log: list[Any]
    adapter_fields: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "model": self.model,
            "user_message": self.user_message,
            "response": self.response,
            "state": self.state,
            "activity_log": self.activity_log,
            "adapter_fields": self.adapter_fields,
        }


def canonical_bytes(body: EpisodeBody) -> bytes:
    return json.dumps(
        body.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_digest(body: EpisodeBody) -> str:
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _encode_component(value: str | int) -> str:
    return base64.urlsafe_b64encode(str(value).encode("utf-8")).decode("ascii").rstrip("=")


def _decode_component(value: str) -> str:
    if not value or "=" in value:
        raise ContractError("reference components must use unpadded URL-safe base64")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ContractError("reference contains an invalid encoded component") from exc
    if _encode_component(decoded) != value:
        raise ContractError("reference component is not canonically encoded")
    return decoded


def _encoded_tuple(*values: str | int) -> str:
    return ".".join(_encode_component(value) for value in values)


def _decoded_tuple(value: str, expected_length: int) -> tuple[str, ...]:
    components = value.split(".")
    if len(components) != expected_length:
        raise ContractError(f"reference component must contain {expected_length} values")
    return tuple(_decode_component(component) for component in components)


def _require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{name} must be a non-empty string")
    return value


def validate_corpus_id(value: str) -> str:
    if not isinstance(value, str) or _CORPUS_ID_PATTERN.fullmatch(value) is None:
        raise ContractError(
            "corpus_id must contain only URL-safe letters, digits, '.', '_', '~', or '-'"
        )
    return value


def _require_corpus_id(value: str) -> str:
    return validate_corpus_id(value)


def _require_positive_version(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class EpisodeReference:
    corpus_id: str
    session_id: str
    episode_id: str

    def __post_init__(self) -> None:
        _require_corpus_id(self.corpus_id)

    @classmethod
    def build(
        cls,
        *,
        corpus_id: str,
        source_id: str,
        native_session_id: str,
        canonicalization_version: int,
        boundary_version: int,
        event_token: str,
        content_digest: str,
    ) -> EpisodeReference:
        _require_corpus_id(corpus_id)
        _require_text("source_id", source_id)
        _require_text("native_session_id", native_session_id)
        _require_text("event_token", event_token)
        _require_positive_version("canonicalization_version", canonicalization_version)
        _require_positive_version("boundary_version", boundary_version)
        if not re.fullmatch(r"[0-9a-f]{64}", content_digest):
            raise ContractError("content_digest must be 64 lowercase hexadecimal characters")
        return cls(
            corpus_id=corpus_id,
            session_id=_encoded_tuple(source_id, native_session_id),
            episode_id=_encoded_tuple(
                canonicalization_version,
                boundary_version,
                event_token,
                content_digest,
            ),
        )

    @classmethod
    def parse(cls, episode_ref: str) -> EpisodeReference:
        if not isinstance(episode_ref, str):
            raise ContractError("episode_ref must be a string")
        if any(character.isspace() for character in episode_ref):
            raise ContractError("episode_ref must not contain whitespace")
        try:
            parsed = urlsplit(episode_ref)
        except ValueError as exc:
            raise ContractError("episode_ref is malformed") from exc
        segments = parsed.path.split("/")
        if (
            parsed.scheme != "episode"
            or len(segments) != 3
            or segments[0]
            or not all(segments[1:])
            or parsed.query
            or parsed.fragment
            or "@" in parsed.netloc
            or ":" in parsed.netloc
        ):
            raise ContractError("episode_ref must have scheme episode and exactly two path segments")

        reference = cls(parsed.netloc, segments[1], segments[2])
        source_id, native_session_id = _decoded_tuple(reference.session_id, 2)
        canonicalization, boundary, event_token, digest = _decoded_tuple(
            reference.episode_id, 4
        )
        _require_text("source_id", source_id)
        _require_text("native_session_id", native_session_id)
        _require_text("event_token", event_token)
        reference._parse_version("canonicalization_version", canonicalization)
        reference._parse_version("boundary_version", boundary)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ContractError("content digest must be 64 lowercase hexadecimal characters")
        return reference

    @staticmethod
    def _parse_version(name: str, value: str) -> int:
        if not value.isascii() or not value.isdecimal():
            raise ContractError(f"{name} must be a positive integer")
        version = int(value)
        return _require_positive_version(name, version)

    @property
    def source_id(self) -> str:
        return _decoded_tuple(self.session_id, 2)[0]

    @property
    def native_session_id(self) -> str:
        return _decoded_tuple(self.session_id, 2)[1]

    @property
    def canonicalization_version(self) -> int:
        value = _decoded_tuple(self.episode_id, 4)[0]
        return self._parse_version("canonicalization_version", value)

    @property
    def boundary_version(self) -> int:
        value = _decoded_tuple(self.episode_id, 4)[1]
        return self._parse_version("boundary_version", value)

    @property
    def event_token(self) -> str:
        return _decoded_tuple(self.episode_id, 4)[2]

    @property
    def content_digest(self) -> str:
        return _decoded_tuple(self.episode_id, 4)[3]

    def __str__(self) -> str:
        return f"episode://{self.corpus_id}/{self.session_id}/{self.episode_id}"


@dataclass(frozen=True)
class EpisodeIdentity:
    reference: EpisodeReference
    body_digest: str

    @property
    def episode_ref(self) -> str:
        return str(self.reference)


def build_identity(
    *,
    corpus_id: str,
    source_id: str,
    native_session_id: str,
    event_token: str,
    canonicalization_version: int,
    boundary_version: int,
    body: EpisodeBody,
) -> EpisodeIdentity:
    digest = content_digest(body)
    reference = EpisodeReference.build(
        corpus_id=corpus_id,
        source_id=source_id,
        native_session_id=native_session_id,
        canonicalization_version=canonicalization_version,
        boundary_version=boundary_version,
        event_token=event_token,
        content_digest=digest,
    )
    return EpisodeIdentity(reference=reference, body_digest=digest)


def reference_key(episode_ref: str | EpisodeReference) -> str:
    value = str(episode_ref)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SearchRequest:
    query: str
    corpus_ids: tuple[str, ...]
    limit: int
    strategy: str
    contract_version: int = CONTRACT_VERSION

    @classmethod
    def create(
        cls,
        query: str,
        corpus_ids: Iterable[str],
        *,
        limit: int = 10,
        strategy: str = STRATEGY,
        contract_version: int = CONTRACT_VERSION,
    ) -> SearchRequest:
        if not isinstance(query, str) or not (clean_query := query.strip()):
            raise ContractError("query must be a non-empty string")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise ContractError(f"limit must be an integer from 1 to {MAX_LIMIT}")
        if not isinstance(strategy, str) or not strategy:
            raise ContractError("strategy must be a non-empty string")
        if (
            isinstance(contract_version, bool)
            or not isinstance(contract_version, int)
            or contract_version != CONTRACT_VERSION
        ):
            raise ContractError(f"contract_version must be {CONTRACT_VERSION}")
        try:
            corpora = tuple(corpus_ids)
        except TypeError as exc:
            raise ContractError("corpus_ids must be an iterable of strings") from exc
        if not corpora:
            raise ContractError("corpus_ids must contain concrete corpus identifiers")
        for corpus_id in corpora:
            _require_corpus_id(corpus_id)
            if corpus_id.lower() == "all":
                raise ContractError("corpus_ids must contain concrete corpus identifiers")
        if len(set(corpora)) != len(corpora):
            raise ContractError("corpus_ids must be unique")
        return cls(clean_query, corpora, limit, strategy, contract_version)


@dataclass(frozen=True)
class ProviderCapabilities:
    contract_versions: tuple[int, ...] = (CONTRACT_VERSION,)
    strategies: tuple[str, ...] = (STRATEGY,)
    supports_facets: bool = False
    supports_continuation: bool = False
    max_limit: int = MAX_LIMIT

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_versions": list(self.contract_versions),
            "strategies": list(self.strategies),
            "supports_facets": self.supports_facets,
            "supports_continuation": self.supports_continuation,
            "max_limit": self.max_limit,
        }
