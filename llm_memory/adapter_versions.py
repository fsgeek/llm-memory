from __future__ import annotations


SUPPORTED_SEMANTIC_VERSIONS = {
    "taste_open_jsonl": frozenset({(1, 1)}),
    "gateway_jsonl": frozenset({(1, 1)}),
    "claude_code_jsonl": frozenset({(1, 1)}),
}


def supports_semantic_versions(
    adapter: str, *, boundary_version: int, canonicalization_version: int
) -> bool:
    return (boundary_version, canonicalization_version) in (
        SUPPORTED_SEMANTIC_VERSIONS.get(adapter) or ()
    )
