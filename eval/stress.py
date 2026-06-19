"""Falsification probes against the conversation-inclusive index. Not a pass/fail
gate — a characterization. Two questions Phase 1's 5/5 could not answer:

  (1) MARGIN: when a logged-zero query now "passes", how decisively does the
      originating turn win? rank + BM25 gap to its neighbours. A rank-3 squeaker
      means mild corpus growth would push it out -> temporal filtering matters soon.

  (2) PARAPHRASE: rewrite a passing query to share NO content words with the
      target turn. BM25 is purely lexical; if it now misses, that is the honest
      edge where embeddings/RAG would eventually earn their keep.

Run: uv run python eval/stress.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from llm_memory.db import get_database
from llm_memory.search import search

SPEC = Path(__file__).parent / "queries.yaml"

# Paraphrases: same MEANING as the original failing query, deliberately avoiding
# the content words that appear in the target turn. Tests lexical vs. semantic reach.
PARAPHRASES = {
    "recognition enhancement": "being seen more clearly improves the bond",
    "clock time on restart": "what wall-clock moment it is after waking again",
    "narrates its own trustedness": "describes how worthy of trust it has become",
}


def rank_of(hits, expected):
    cyc = [h["cycle"] for h in hits]
    for want in expected:
        if want in cyc:
            return cyc.index(want), want
    return None, None


def margin_probe(db, spec, k_show=10):
    print("=== MARGIN OF VICTORY ===")
    for item in spec["queries"]:
        q, expected = item["query"], item["expected"]
        hits = search(db, q, limit=k_show)
        rank, want = rank_of(hits, expected)
        scores = [h["score"] for h in hits]
        if rank is None:
            print(f"\n{q!r}\n  expected {expected} NOT in top-{k_show}  (scores {scores[:5]}...)")
            continue
        s_want = scores[rank]
        s_above = scores[rank - 1] if rank > 0 else None
        s_below = scores[rank + 1] if rank + 1 < len(scores) else None
        gap_up = f"{s_above - s_want:+.3f} (rank {rank-1} above)" if s_above is not None else "— (is rank 0)"
        gap_dn = f"{s_want - s_below:+.3f} (rank {rank+1} below)" if s_below is not None else "— (last)"
        print(f"\n{q!r}")
        print(f"  cy{want} at RANK {rank}  score={s_want:.3f}")
        print(f"  gap to neighbour above: {gap_up}")
        print(f"  gap to neighbour below: {gap_dn}")
        print(f"  full top-{k_show} cycles: {[h['cycle'] for h in hits]}")


def paraphrase_probe(db, spec):
    print("\n=== PARAPHRASE STRESS (lexical -> semantic) ===")
    by_q = {it["query"]: it["expected"] for it in spec["queries"]}
    for orig, para in PARAPHRASES.items():
        expected = by_q[orig]
        hits = search(db, para, limit=10)
        rank, want = rank_of(hits, expected)
        verdict = f"FOUND at rank {rank} (cy{want})" if rank is not None else "MISS (not in top-10)"
        print(f"\n  orig : {orig!r}  -> expected {expected}")
        print(f"  para : {para!r}")
        print(f"  -> {verdict}; top cycles {[h['cycle'] for h in hits]}")


def main():
    spec = yaml.safe_load(SPEC.read_text())
    db = get_database()
    margin_probe(db, spec)
    paraphrase_probe(db, spec)


if __name__ == "__main__":
    main()
