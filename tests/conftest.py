import pytest

from llm_memory import adapter_versions


@pytest.fixture
def enable_semantic_version(monkeypatch):
    def enable(adapter: str, *, boundary: int = 1, canonicalization: int = 1):
        supported = adapter_versions.SUPPORTED_SEMANTIC_VERSIONS[adapter]
        monkeypatch.setitem(
            adapter_versions.SUPPORTED_SEMANTIC_VERSIONS,
            adapter,
            supported | {(boundary, canonicalization)},
        )

    return enable
