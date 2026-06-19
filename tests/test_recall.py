import json

from llm_memory.db import get_database
from llm_memory.index import EPISODES, ensure_index
from llm_memory.ingest import ingest_file
from llm_memory.recall import recall


def test_recall_returns_full_episode_by_key(tmp_path):
    """recall is the second half of the reach: search says WHICH cycle, recall
    returns that cycle IN FULL. The response here is longer than search's 200-char
    snippet, so recalling the whole thing proves recall reads the episode rather
    than echoing a teaser (the filesystem-fallback this tool exists to prevent)."""
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    key = "900030"
    long_response = "the marker " + "provenance " * 60  # > 200 chars
    try:
        rec = {
            "cycle": 900030,
            "user_message": "what did you mean?",
            "raw_output": {"response": long_response},
            "state": {"observation": "kept whole"},
        }
        p = tmp_path / "r.jsonl"
        p.write_text(json.dumps(rec))
        ingest_file(db, p)

        episode = recall(db, key)

        assert episode is not None
        assert episode["_key"] == key
        assert episode["response"] == long_response  # full, not truncated
        assert episode["user_message"] == "what did you mean?"
    finally:
        if col.has(key):
            col.delete(key)


def test_recall_returns_none_for_missing_key():
    """A reach for a key that isn't there is a clean miss, not an error."""
    db = get_database()
    assert recall(db, "no-such-key-900031") is None
