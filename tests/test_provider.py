from __future__ import annotations

import pytest

from llm_memory.contract import ProviderCapabilities, SearchRequest
from llm_memory.provider import (
    ProviderDescriptor,
    ProviderMeasurement,
    ProviderUnavailable,
    ProviderUnsupported,
    PurgeScope,
)


def test_provider_descriptor_declares_retrieval_basis():
    descriptor = ProviderDescriptor(
        provider="sqlite",
        implementation_version="1",
        strategies=("lexical_bm25_fts5_porter_unicode61_v1",),
        analyzer="porter unicode61 remove_diacritics 2",
        indexed_fields=("user_message", "response", "state_text"),
        match_semantics="analyzed_any_segment_phrase",
        score_ordering="normalized_desc_episode_ref_asc",
        raw_score_polarity="lower_is_better",
    )

    assert descriptor.as_dict() == {
        "provider": "sqlite",
        "implementation_version": "1",
        "strategies": ("lexical_bm25_fts5_porter_unicode61_v1",),
        "analyzer": "porter unicode61 remove_diacritics 2",
        "indexed_fields": ("user_message", "response", "state_text"),
        "match_semantics": "analyzed_any_segment_phrase",
        "score_ordering": "normalized_desc_episode_ref_asc",
        "raw_score_polarity": "lower_is_better",
    }


def test_purge_scope_requires_corpus_for_source():
    with pytest.raises(ValueError, match="source_id requires corpus_id"):
        PurgeScope(source_id="source-a")


def test_provider_measurement_and_errors_are_provider_neutral():
    measurement = ProviderMeasurement(
        provider="sqlite",
        standing="available",
        observations={"episode_documents": 3},
    )

    assert measurement.provider == "sqlite"
    assert measurement.observations == {"episode_documents": 3}
    assert issubclass(ProviderUnavailable, RuntimeError)
    assert issubclass(ProviderUnsupported, RuntimeError)


def test_provider_capabilities_accept_explicit_strategies_without_changing_default():
    sqlite_strategy = "lexical_bm25_fts5_porter_unicode61_v1"

    assert ProviderCapabilities(strategies=(sqlite_strategy,)).as_dict()["strategies"] == [
        sqlite_strategy
    ]
    assert SearchRequest.create("reason", ["local"]).strategy == (
        "lexical_bm25_text_en_v1"
    )
