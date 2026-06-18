import json

from llm_memory.db import get_database
from llm_memory.index import EPISODES, ensure_index
from llm_memory.ingest import ingest_file, record_to_episode


def test_record_to_episode_extracts_conversation_and_state():
    record = {
        "cycle": 457,
        "timestamp": "2026-03-31T05:59:03Z",
        "model": "claude-haiku",
        "experiment_label": "taste_open",
        "user_message": "what did you mean by recognition?",
        "raw_output": {"response": "I meant recognition enhancement.", "updated_regions": []},
        "response_text": "I meant recognition enhancement.",
        "state": {
            "observation": "drowning wall",
            "_activity_log": [{"cycle": 457, "tool": "search_memory"}],
        },
    }

    ep = record_to_episode(record, source_file="taste_open_20260331.jsonl")

    assert ep["_key"] == "000457"
    assert ep["cycle"] == 457
    assert ep["user_message"] == "what did you mean by recognition?"
    assert ep["response"] == "I meant recognition enhancement."
    assert "drowning wall" in ep["state_text"]
    # _activity_log is tool-trace (it echoes the instance's own queries); it must
    # NOT leak into the searchable state_text, or it confounds recall.
    assert "search_memory" not in ep["state_text"]
    assert ep["activity_log"] == [{"cycle": 457, "tool": "search_memory"}]
    assert ep["source_file"] == "taste_open_20260331.jsonl"


def test_ingest_file_loads_records(tmp_path):
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    keys = ["900001", "900002"]
    try:
        records = [
            {"cycle": 900001, "user_message": "hello", "raw_output": {"response": "hi there"}, "state": {"x": "alpha"}},
            {"cycle": 900002, "user_message": "bye", "raw_output": {"response": "later"}, "state": {"x": "beta"}},
        ]
        p = tmp_path / "sample.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in records))

        n = ingest_file(db, p)

        assert n == 2
        doc = col.get("900001")
        assert doc["response"] == "hi there"
        assert doc["user_message"] == "hello"
    finally:
        for k in keys:
            if col.has(k):
                col.delete(k)
