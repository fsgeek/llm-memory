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
