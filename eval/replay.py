"""Replay the logged-zero queries against the conversation-inclusive index and
report how many now return the originating turn. Run: uv run python eval/replay.py
(assumes the corpus has been ingested)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from llm_memory.db import get_database
from llm_memory.evaluate import hit_at_k
from llm_memory.search import search

SPEC = Path(__file__).parent / "queries.yaml"


def main():
    spec = yaml.safe_load(SPEC.read_text())
    k = spec.get("k", 3)
    db = get_database()
    passed = 0
    for item in spec["queries"]:
        query, expected = item["query"], item["expected"]
        cycles = [h["cycle"] for h in search(db, query, limit=k)]
        ok = hit_at_k(cycles, expected, k=k)
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {query!r}  top{k}={cycles}  expected~{expected}  (baseline: 0)")
    total = len(spec["queries"])
    print(f"\n{passed}/{total} previously-zero queries now return the originating turn in top-{k}.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
