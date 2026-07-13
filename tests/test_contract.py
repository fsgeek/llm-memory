from dataclasses import replace

import pytest

from llm_memory.contract import (
    ContractError,
    EpisodeBody,
    EpisodeReference,
    SearchRequest,
    build_identity,
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
    changed_boundary = build_identity(body=BODY, **(base | {"boundary_version": 2}))
    assert len(
        {original.episode_ref, changed_body.episode_ref, changed_boundary.episode_ref}
    ) == 3


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
    assert len(reference_key(identity.episode_ref)) == 64
    assert reference_key(identity.episode_ref) not in identity.episode_ref


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
