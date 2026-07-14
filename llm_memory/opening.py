from __future__ import annotations

from typing import Any, Callable

from llm_memory.adapters import EpisodeRecord, get_adapter
from llm_memory.contract import (
    CONTRACT_VERSION,
    ContractError,
    EpisodeReference,
    OpenStanding,
    SourceStanding,
)
from llm_memory.enrollment import EnrollmentRegistry, SourceEnrollment


SupersessionResolver = Callable[[SourceEnrollment, str], str | None]


def _open_response(
    episode_ref: str, standing: OpenStanding, **details: Any
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "episode_ref": episode_ref,
        "standing": standing.value,
        **details,
    }


def _opening_enrollment(
    registry: EnrollmentRegistry,
    reference: EpisodeReference,
    active_corpus_ids: list[str] | tuple[str, ...],
) -> SourceEnrollment:
    if not isinstance(registry, EnrollmentRegistry):
        raise ContractError("registry must be an EnrollmentRegistry")
    if not isinstance(active_corpus_ids, (list, tuple)):
        raise ContractError("active_corpus_ids must be a list or tuple")
    if active_corpus_ids.count(reference.corpus_id) != 1:
        raise ContractError("episode corpus must appear exactly once in active corpus scope")

    declarations = tuple(
        source
        for source in registry.sources
        if source.enabled
        and source.corpus_id == reference.corpus_id
        and source.source_id == reference.source_id
    )
    if len(declarations) != 1:
        raise ContractError("episode source is not uniquely enrolled and enabled")
    return declarations[0]


def _available_opening(
    episode_ref: str,
    enrollment: SourceEnrollment,
    implementation_version: str,
    episode: EpisodeRecord,
) -> dict[str, Any]:
    return _open_response(
        episode_ref,
        OpenStanding.AVAILABLE,
        **episode.body.as_dict(),
        provenance={
            "corpus_id": enrollment.corpus_id,
            "source_id": enrollment.source_id,
            "adapter": enrollment.adapter,
            "implementation_version": implementation_version,
            "canonicalization_version": enrollment.canonicalization_version,
            "boundary_version": enrollment.boundary_version,
            "native_event_id": episode.native_event_id,
            "source_position": episode.source_position,
            "content_digest": episode.identity.body_digest,
        },
    )


def open_episode(
    registry: EnrollmentRegistry,
    episode_ref: str,
    active_corpus_ids: list[str] | tuple[str, ...],
    resolve_supersession: SupersessionResolver,
) -> dict[str, object]:
    reference = EpisodeReference.parse(episode_ref)
    enrollment = _opening_enrollment(registry, reference, active_corpus_ids)
    try:
        adapter = get_adapter(enrollment.adapter)
    except ContractError:
        return _open_response(episode_ref, OpenStanding.UNSUPPORTED_ADAPTER)

    try:
        members = adapter.members(enrollment)
    except OSError:
        return _open_response(episode_ref, OpenStanding.SOURCE_UNAVAILABLE)

    available_episodes: list[EpisodeRecord] = []
    standings: list[SourceStanding] = []
    try:
        for member in members:
            scan = adapter.scan(enrollment, member)
            standings.append(scan.source_standing)
            if scan.source_standing is SourceStanding.AVAILABLE:
                available_episodes.extend(scan.episodes)
    except OSError:
        return _open_response(episode_ref, OpenStanding.SOURCE_UNAVAILABLE)

    by_ref = {
        episode.identity.episode_ref: episode for episode in available_episodes
    }
    exact = by_ref.get(episode_ref)
    if exact is not None:
        return _available_opening(
            episode_ref,
            enrollment,
            adapter.implementation_version,
            exact,
        )

    if SourceStanding.MALFORMED in standings:
        return _open_response(episode_ref, OpenStanding.MALFORMED_SOURCE)
    if any(standing is not SourceStanding.AVAILABLE for standing in standings):
        return _open_response(episode_ref, OpenStanding.SOURCE_UNAVAILABLE)

    replacement_ref = resolve_supersession(enrollment, episode_ref)
    if replacement_ref in by_ref:
        return _open_response(
            episode_ref,
            OpenStanding.SUPERSEDED,
            replacement_ref=replacement_ref,
        )

    same_event = any(
        episode.identity.reference.native_session_id == reference.native_session_id
        and episode.identity.reference.event_token == reference.event_token
        for episode in available_episodes
    )
    standing = OpenStanding.CONTENT_MISMATCH if same_event else OpenStanding.MISSING
    return _open_response(episode_ref, standing)
