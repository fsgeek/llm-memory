import json

from llm_memory.db import get_database
from llm_memory.index import EPISODES, ensure_index
from llm_memory.ingest import ingest_file
from llm_memory.search import search


def test_search_finds_a_phrase_the_instance_said_in_response(tmp_path):
    """The canonical manufactured-silence case: a phrase that lives only in the
    conversational `response` field must be findable."""
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    key = "900010"
    try:
        # Unique marker phrase so this is deterministic regardless of what else
        # lives in the shared episodes collection (e.g. an ingested corpus). The
        # phrase exists ONLY in the conversational `response` field.
        rec = {
            "cycle": 900010,
            "user_message": "the human asks about trust",
            "raw_output": {"response": "I meant the heliotrope cantilever marker specifically"},
            "state": {"x": "unrelated noise"},
        }
        p = tmp_path / "s.jsonl"
        p.write_text(json.dumps(rec))
        ingest_file(db, p)

        results = search(db, "heliotrope cantilever", limit=5)

        assert 900010 in [r["cycle"] for r in results]
    finally:
        if col.has(key):
            col.delete(key)


def test_scope_partitions_corpora_by_experiment_label(tmp_path):
    """Two episodes share one unique marker phrase but belong to different
    corpora. `scope="claude_code"` must surface only the claude_code episode, so
    a live-session query cannot reach taste_open episodes (and vice versa)."""
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    keys = ["900020", "900021"]
    try:
        marker = "xylophone perimeter sandbar"  # unique, shared by both episodes
        recs = [
            {
                "cycle": 900020,
                "experiment_label": "claude_code",
                "user_message": "live session turn",
                "raw_output": {"response": f"discussing the {marker} at length"},
                "state": {},
            },
            {
                "cycle": 900021,
                "experiment_label": "taste_open",
                "user_message": "eval corpus turn",
                "raw_output": {"response": f"the {marker} appears here too"},
                "state": {},
            },
        ]
        p = tmp_path / "two.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in recs))
        ingest_file(db, p)

        all_cycles = [r["cycle"] for r in search(db, marker, scope="all", limit=10)]
        assert 900020 in all_cycles and 900021 in all_cycles

        cc_cycles = [r["cycle"] for r in search(db, marker, scope="claude_code", limit=10)]
        assert 900020 in cc_cycles
        assert 900021 not in cc_cycles
    finally:
        for k in keys:
            if col.has(k):
                col.delete(k)
