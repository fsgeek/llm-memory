from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from llm_memory.adapters import EpisodeRecord, SourceMember
from llm_memory.contract import reference_key
from llm_memory.enrollment import SourceEnrollment


CONTRACT_EPISODES = "episodic_contract_episodes"
CONTRACT_VIEW = "episodic_contract_search"
SOURCE_STATES = "episodic_contract_sources"
SUPERSESSIONS = "episodic_contract_supersessions"

_INDEXED_FIELDS = ("user_message", "response", "state_text")
_ANALYZER = "text_en"


class GenerationStateConflict(ValueError):
    pass


class GenerationDocumentConflict(ValueError):
    def __init__(self, document: dict[str, Any]):
        self.error_position = document["source_position"]["start"]
        super().__init__(f"conflicting generation document {document['_key']!r}")


def _view_properties() -> dict[str, Any]:
    return {
        "links": {
            CONTRACT_EPISODES: {
                "fields": {
                    field: {"analyzers": [_ANALYZER]}
                    for field in _INDEXED_FIELDS
                }
            }
        }
    }


def ensure_contract_index(db) -> None:
    for collection_name in (
        CONTRACT_EPISODES,
        SOURCE_STATES,
        SUPERSESSIONS,
    ):
        if not db.has_collection(collection_name):
            db.create_collection(collection_name)

    properties = _view_properties()
    if CONTRACT_VIEW in {view["name"] for view in db.views()}:
        db.update_arangosearch_view(CONTRACT_VIEW, properties)
    else:
        db.create_arangosearch_view(CONTRACT_VIEW, properties=properties)


def generation_storage_key(generation_id: str, episode_ref: str) -> str:
    digest = hashlib.sha256()
    digest.update(generation_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(episode_ref.encode("utf-8"))
    return digest.hexdigest()


def _source_state_key(corpus_id: str, source_id: str, member_id: str) -> str:
    identity = f"{corpus_id}/{source_id}/{member_id}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _episode_document(
    enrollment: SourceEnrollment,
    member: SourceMember,
    generation_id: str,
    episode: EpisodeRecord,
) -> dict[str, Any]:
    episode_ref = episode.identity.episode_ref
    return {
        "_key": generation_storage_key(generation_id, episode_ref),
        "episode_ref": episode_ref,
        "reference_key": reference_key(episode_ref),
        "corpus_id": enrollment.corpus_id,
        "source_id": enrollment.source_id,
        "member_id": member.member_id,
        "generation_id": generation_id,
        "canonicalization_version": enrollment.canonicalization_version,
        "boundary_version": enrollment.boundary_version,
        "body_digest": episode.identity.body_digest,
        "native_event_id": episode.native_event_id,
        "source_position": episode.source_position,
        **episode.body.as_dict(),
        "state_text": episode.state_text,
    }


def write_generation(
    db,
    enrollment: SourceEnrollment,
    member: SourceMember,
    generation_id: str,
    episodes: Iterable[EpisodeRecord],
    *,
    expected_state: dict | None = None,
) -> int:
    state_key = _source_state_key(
        enrollment.corpus_id, enrollment.source_id, member.member_id
    )
    current_state = db.collection(SOURCE_STATES).get(state_key)
    if (
        current_state
        and current_state.get("staging_generation_id") == generation_id
        and (
            current_state.get("staging_canonicalization_version")
            != enrollment.canonicalization_version
            or current_state.get("staging_boundary_version")
            != enrollment.boundary_version
        )
    ):
        raise ValueError("staging generation semantic versions conflict")

    collection = db.collection(CONTRACT_EPISODES)
    documents = [
        _episode_document(enrollment, member, generation_id, episode)
        for episode in episodes
    ]
    for document in documents:
        existing = collection.get(document["_key"])
        if existing is None:
            collection.insert(document)
            continue
        stored = {
            key: value
            for key, value in existing.items()
            if key not in {"_id", "_rev"}
        }
        if stored != document:
            raise GenerationDocumentConflict(document)

    bind_vars = {
        "@episodes": CONTRACT_EPISODES,
        "@states": SOURCE_STATES,
        "key": state_key,
        "identity": {
            "_key": state_key,
            "corpus_id": enrollment.corpus_id,
            "source_id": enrollment.source_id,
            "member_id": member.member_id,
            "active_generation_id": None,
            "staging_generation_id": generation_id,
            "staging_episode_count": None,
            "canonicalization_version": None,
            "boundary_version": None,
            "staging_canonicalization_version": enrollment.canonicalization_version,
            "staging_boundary_version": enrollment.boundary_version,
        },
        "generation_id": generation_id,
        "corpus_id": enrollment.corpus_id,
        "source_id": enrollment.source_id,
        "member_id": member.member_id,
        "canonicalization_version": enrollment.canonicalization_version,
        "boundary_version": enrollment.boundary_version,
    }
    count_query = """
        LET staged_count = LENGTH(
            FOR episode IN @@episodes
                FILTER episode.corpus_id == @corpus_id
                FILTER episode.source_id == @source_id
                FILTER episode.member_id == @member_id
                FILTER episode.generation_id == @generation_id
                RETURN 1
        )
    """
    if expected_state is None:
        query = count_query + """
        UPSERT { _key: @key }
            INSERT MERGE(@identity, { staging_episode_count: staged_count })
            UPDATE {
                staging_generation_id: @generation_id,
                staging_episode_count: staged_count,
                staging_canonicalization_version: @canonicalization_version,
                staging_boundary_version: @boundary_version
            }
            IN @@states
        RETURN NEW._key
        """
    else:
        query = count_query + """
        FOR current IN @@states
            FILTER current._key == @key
            FILTER current._rev == @expected_revision
            FILTER current.active_generation_id == @expected_active_generation_id
            FILTER current.build_generation_id == @expected_build_generation_id
            FILTER current.build_cursor == @expected_build_cursor
            FILTER current.staging_generation_id == @expected_staging_generation_id
            FILTER current.staging_episode_count == @expected_staging_episode_count
            UPDATE current WITH {
                staging_generation_id: @generation_id,
                staging_episode_count: staged_count,
                staging_canonicalization_version: @canonicalization_version,
                staging_boundary_version: @boundary_version
            } IN @@states
            RETURN NEW._key
        """
        bind_vars.pop("identity")
        bind_vars.update(
            {
                "expected_revision": expected_state.get("_rev"),
                "expected_active_generation_id": expected_state.get(
                    "active_generation_id"
                ),
                "expected_build_generation_id": expected_state.get(
                    "build_generation_id"
                ),
                "expected_build_cursor": expected_state.get("build_cursor"),
                "expected_staging_generation_id": expected_state.get(
                    "staging_generation_id"
                ),
                "expected_staging_episode_count": expected_state.get(
                    "staging_episode_count"
                ),
            }
        )
    published = list(db.aql.execute(query, bind_vars=bind_vars))
    if not published:
        raise GenerationStateConflict(
            f"generation {generation_id!r} lost staging ownership"
        )
    return len(documents)


def activate_generation(
    db,
    enrollment: SourceEnrollment,
    member: SourceMember,
    generation_id: str,
    state: dict,
    *,
    expected_state: dict | None = None,
) -> None:
    state_key = _source_state_key(
        enrollment.corpus_id, enrollment.source_id, member.member_id
    )
    authoritative_state = {
        **state,
        "corpus_id": enrollment.corpus_id,
        "source_id": enrollment.source_id,
        "member_id": member.member_id,
        "active_generation_id": generation_id,
        "staging_generation_id": None,
        "staging_canonicalization_version": None,
        "staging_boundary_version": None,
        "canonicalization_version": enrollment.canonicalization_version,
        "boundary_version": enrollment.boundary_version,
    }
    activated = list(
        db.aql.execute(
            """
            LET episode_count = LENGTH(
                FOR episode IN @@episodes
                    FILTER episode.corpus_id == @corpus_id
                    FILTER episode.source_id == @source_id
                    FILTER episode.member_id == @member_id
                    FILTER episode.generation_id == @generation_id
                    RETURN 1
            )
            FOR current IN @@states
                FILTER current._key == @key
                FILTER current.staging_generation_id == @generation_id
                FILTER current.staging_episode_count == episode_count
                FILTER @expected_revision == null OR current._rev == @expected_revision
                FILTER @guard_transition == false OR (
                    current.active_generation_id == @expected_active_generation_id
                    AND current.build_generation_id == @expected_build_generation_id
                    AND current.build_cursor == @expected_build_cursor
                )
                UPDATE current WITH MERGE(
                    @state,
                    {
                        episode_count,
                        staging_episode_count: null
                    }
                ) IN @@states
                RETURN NEW._key
            """,
            bind_vars={
                "@episodes": CONTRACT_EPISODES,
                "@states": SOURCE_STATES,
                "key": state_key,
                "corpus_id": enrollment.corpus_id,
                "source_id": enrollment.source_id,
                "member_id": member.member_id,
                "generation_id": generation_id,
                "state": authoritative_state,
                "expected_revision": (
                    expected_state.get("_rev") if expected_state else None
                ),
                "guard_transition": expected_state is not None,
                "expected_active_generation_id": (
                    expected_state.get("active_generation_id")
                    if expected_state
                    else None
                ),
                "expected_build_generation_id": (
                    expected_state.get("build_generation_id")
                    if expected_state
                    else None
                ),
                "expected_build_cursor": (
                    expected_state.get("build_cursor") if expected_state else None
                ),
            },
        )
    )
    if not activated:
        raise ValueError(f"generation {generation_id!r} is not fully staged")


def active_states(db, corpus_ids: tuple[str, ...]) -> tuple[dict, ...]:
    if not corpus_ids:
        return ()
    return tuple(
        db.aql.execute(
            """
            FOR state IN @@states
                FILTER state.corpus_id IN @corpus_ids
                FILTER state.active_generation_id != null
                LET stored_episode_count = LENGTH(
                    FOR episode IN @@episodes
                        FILTER episode.corpus_id == state.corpus_id
                        FILTER episode.source_id == state.source_id
                        FILTER episode.member_id == state.member_id
                        FILTER episode.generation_id == state.active_generation_id
                        RETURN 1
                )
                SORT state.corpus_id, state.source_id, state.member_id
                RETURN MERGE(
                    UNSET(state, "_id", "_rev"),
                    {
                        stored_episode_count,
                        active_generation_backed: stored_episode_count == state.episode_count
                    }
                )
            """,
            bind_vars={
                "@states": SOURCE_STATES,
                "@episodes": CONTRACT_EPISODES,
                "corpus_ids": list(corpus_ids),
            },
        )
    )


def delete_generation(
    db,
    corpus_id: str,
    source_id: str,
    member_id: str,
    generation_id: str,
) -> int:
    removed = list(
        db.aql.execute(
            """
            LET generation_is_active = FIRST(
                FOR state IN @@states
                    FILTER state.corpus_id == @corpus_id
                    FILTER state.source_id == @source_id
                    FILTER state.member_id == @member_id
                    FILTER state.active_generation_id == @generation_id
                    LIMIT 1
                    RETURN true
            )
            FOR episode IN @@episodes
                FILTER generation_is_active != true
                FILTER episode.corpus_id == @corpus_id
                FILTER episode.source_id == @source_id
                FILTER episode.member_id == @member_id
                FILTER episode.generation_id == @generation_id
                REMOVE episode IN @@episodes
                RETURN OLD._key
            """,
            bind_vars={
                "@episodes": CONTRACT_EPISODES,
                "@states": SOURCE_STATES,
                "corpus_id": corpus_id,
                "source_id": source_id,
                "member_id": member_id,
                "generation_id": generation_id,
            },
        )
    )
    return len(removed)
