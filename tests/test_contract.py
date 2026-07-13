import hashlib
from dataclasses import replace

import pytest

from llm_memory.contract import (
    ContractError,
    EpisodeBody,
    EpisodeReference,
    FreshnessStanding,
    IndexStanding,
    OpenStanding,
    ProviderCapabilities,
    SearchRequest,
    SourceStanding,
    TotalStanding,
    build_identity,
    canonical_bytes,
    reference_key,
)


BODY = EpisodeBody(
    timestamp="2026-07-12T18:29:10Z",
    model="claude-test",
    user_message="question",
    response="answer",
    state={"status": "observed"},
    activity_log=[],
    adapter_fields={},
)


def test_identity_round_trips_and_survives_implementation_release():
    first = build_identity(
        corpus_id="corpus-a",
        source_id="source-a",
        native_session_id="session-a",
        event_token="event-a",
        canonicalization_version=1,
        boundary_version=1,
        body=BODY,
    )
    parsed = EpisodeReference.parse(first.episode_ref)
    assert parsed == first.reference
    assert parsed.source_id == "source-a"
    assert parsed.native_session_id == "session-a"
    assert parsed.event_token == "event-a"
    assert parsed.canonicalization_version == 1
    assert parsed.boundary_version == 1
    assert parsed.content_digest == first.body_digest


def test_content_or_semantic_version_change_churns_identity():
    base = dict(
        corpus_id="corpus-a",
        source_id="source-a",
        native_session_id="session-a",
        event_token="event-a",
        canonicalization_version=1,
        boundary_version=1,
    )
    original = build_identity(body=BODY, **base)
    changed_body = build_identity(body=replace(BODY, response="different"), **base)
    changed_canonicalization = build_identity(
        body=BODY, **(base | {"canonicalization_version": 2})
    )
    changed_boundary = build_identity(body=BODY, **(base | {"boundary_version": 2}))
    assert len(
        {
            original.episode_ref,
            changed_body.episode_ref,
            changed_canonicalization.episode_ref,
            changed_boundary.episode_ref,
        }
    ) == 4


def test_reference_key_is_backend_only_sha256():
    identity = build_identity(
        corpus_id="corpus-a",
        source_id="source-a",
        native_session_id="session-a",
        event_token="event-a",
        canonicalization_version=1,
        boundary_version=1,
        body=BODY,
    )
    expected = hashlib.sha256(identity.episode_ref.encode("utf-8")).hexdigest()
    assert reference_key(identity.episode_ref) == expected
    assert len(reference_key(identity.episode_ref)) == 64
    assert reference_key(identity.episode_ref) not in identity.episode_ref


def test_canonical_bytes_are_exact_sorted_compact_utf8_json():
    assert canonical_bytes(BODY) == (
        b'{"activity_log":[],"adapter_fields":{},"model":"claude-test",'
        b'"response":"answer","state":{"status":"observed"},'
        b'"timestamp":"2026-07-12T18:29:10Z","user_message":"question"}'
    )


def test_standing_enums_have_exact_contract_values():
    assert [standing.value for standing in SourceStanding] == [
        "available",
        "unavailable",
        "missing",
        "unknown",
        "unsupported_adapter",
        "malformed",
    ]
    assert [standing.value for standing in IndexStanding] == [
        "available",
        "rebuilding",
        "unavailable",
    ]
    assert [standing.value for standing in FreshnessStanding] == [
        "current",
        "tail_validated",
        "stale",
        "incomplete",
        "unknown",
        "unavailable",
    ]
    assert [standing.value for standing in TotalStanding] == [
        "exact",
        "estimated",
        "lower_bound",
        "unknown",
    ]
    assert [standing.value for standing in OpenStanding] == [
        "available",
        "source_unavailable",
        "missing",
        "content_mismatch",
        "unsupported_adapter",
        "malformed_source",
        "superseded",
    ]


def test_provider_capabilities_are_exact():
    assert ProviderCapabilities().as_dict() == {
        "contract_versions": [1],
        "strategies": ["lexical_bm25_text_en_v1"],
        "supports_facets": False,
        "supports_continuation": False,
        "max_limit": 100,
    }


@pytest.mark.parametrize("limit", [0, 101, True])
def test_search_request_rejects_invalid_limit(limit):
    with pytest.raises(ContractError):
        SearchRequest.create("query", ["corpus-a"], limit=limit)


def test_search_request_requires_concrete_unique_corpora():
    with pytest.raises(ContractError):
        SearchRequest.create("query", [])
    with pytest.raises(ContractError):
        SearchRequest.create("query", ["corpus-a", "corpus-a"])
    with pytest.raises(ContractError):
        SearchRequest.create("query", ["*"])


@pytest.mark.parametrize(
    "episode_ref",
    ["episode://corpus:bad/session/episode", "episode://[/session/episode"],
)
def test_parse_reports_malformed_authority_as_contract_error(episode_ref):
    with pytest.raises(ContractError):
        EpisodeReference.parse(episode_ref)


def test_url_safe_corpus_id_round_trips_through_reference_and_search():
    identity = build_identity(
        corpus_id="Corpus_1.~safe",
        source_id="source-a",
        native_session_id="session-a",
        event_token="event-a",
        canonicalization_version=1,
        boundary_version=1,
        body=BODY,
    )

    assert EpisodeReference.parse(identity.episode_ref) == identity.reference
    assert SearchRequest.create("query", ["Corpus_1.~safe"]).corpus_ids == (
        "Corpus_1.~safe",
    )


@pytest.mark.parametrize(
    "corpus_id", ["", "bad/id", "bad?id", "bad#id", "bad:id", "bad@id", "bad id"]
)
def test_identity_rejects_noncanonical_corpus_id(corpus_id):
    with pytest.raises(ContractError):
        build_identity(
            corpus_id=corpus_id,
            source_id="source-a",
            native_session_id="session-a",
            event_token="event-a",
            canonicalization_version=1,
            boundary_version=1,
            body=BODY,
        )


@pytest.mark.parametrize(
    "corpus_id",
    [
        "",
        "bad%2Fid",
        "bad?id",
        "bad#id",
        "bad:id",
        "bad@id",
        "bad id",
        "bad\nid",
    ],
)
def test_parse_rejects_noncanonical_corpus_id(corpus_id):
    valid_ref = build_identity(
        corpus_id="corpus-a",
        source_id="source-a",
        native_session_id="session-a",
        event_token="event-a",
        canonicalization_version=1,
        boundary_version=1,
        body=BODY,
    ).episode_ref
    episode_ref = valid_ref.replace("corpus-a", corpus_id, 1)
    with pytest.raises(ContractError):
        EpisodeReference.parse(episode_ref)


@pytest.mark.parametrize(
    "corpus_id",
    ["", "bad/id", "bad?id", "bad#id", "bad:id", "bad@id", "bad id", "*", "all"],
)
def test_search_rejects_noncanonical_or_wildcard_corpus_id(corpus_id):
    with pytest.raises(ContractError):
        SearchRequest.create("query", [corpus_id])
