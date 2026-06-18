"""Controlled A/B: replay the logged-zero queries against a STATE-ONLY view (the
status quo that manufactured silence) vs. the CONVERSATION-INCLUSIVE view, in the
same system, on the same ingested corpus. Run: uv run python eval/compare.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from llm_memory.db import get_database
from llm_memory.evaluate import hit_at_k
from llm_memory.index import ANALYZER, EPISODES, VIEW, ensure_index
from llm_memory.search import search

STATE_ONLY_VIEW = "episodes_state_only"
SPEC = Path(__file__).parent / "queries.yaml"


def ensure_state_only_view(db):
    """A view indexing ONLY flattened state — reproduces the original scope."""
    props = {"links": {EPISODES: {"fields": {"state_text": {"analyzers": [ANALYZER]}}}}}
    if STATE_ONLY_VIEW in [v["name"] for v in db.views()]:
        db.update_arangosearch_view(STATE_ONLY_VIEW, props)
    else:
        db.create_arangosearch_view(STATE_ONLY_VIEW, properties=props)


def main():
    spec = yaml.safe_load(SPEC.read_text())
    k = spec.get("k", 3)
    db = get_database()
    ensure_index(db)
    ensure_state_only_view(db)

    state_pass = conv_pass = 0
    for item in spec["queries"]:
        q, expected = item["query"], item["expected"]
        state = [h["cycle"] for h in search(db, q, limit=k, view=STATE_ONLY_VIEW)]
        conv = [h["cycle"] for h in search(db, q, limit=k, view=VIEW)]
        s_ok, c_ok = hit_at_k(state, expected, k), hit_at_k(conv, expected, k)
        state_pass += s_ok
        conv_pass += c_ok
        print(
            f"{q[:40]:40}  state-only: {('HIT ' if s_ok else 'miss'):4} {str(state):16}"
            f"  conv: {('HIT ' if c_ok else 'miss'):4} {conv}"
        )
    n = len(spec["queries"])
    print(f"\nstate-only view        : {state_pass}/{n} return originating turn in top-{k}")
    print(f"conversation-inclusive : {conv_pass}/{n} return originating turn in top-{k}")


if __name__ == "__main__":
    main()
