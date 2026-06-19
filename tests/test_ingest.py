import json

from llm_memory.db import get_database
from llm_memory.index import EPISODES, ensure_index
from llm_memory.ingest import (
    gateway_record_to_episode,
    ingest_file,
    ingest_gateway_file,
    record_to_episode,
)


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


def test_gateway_record_to_episode_maps_exchange():
    """A pichay gateway `request_metrics` event -> one episode. The exchange's
    user_message is the LAST user turn (the prompt this turn answered); the
    response is `response_text` — the words pichay now captures instead of
    dropping. No authored state (Claude Code has none); keyed by session+seq."""
    record = {
        "type": "request_metrics",
        "timestamp": "2026-06-18T20:00:00Z",
        "session_id": "1a5d5635",
        "model": "claude-opus-4-8",
        "messages_full": [
            {"role": "user", "content": "what did I say about provenance?"},
            {"role": "assistant", "content": [{"type": "text", "text": "earlier reply"}]},
            {"role": "user", "content": [{"type": "text", "text": "and manufactured silence?"}]},
        ],
        "response_text": "You said attribution corruption is a kind of silence.",
        "usage": {"output_tokens": 9},
    }

    ep = gateway_record_to_episode(record, seq=3, source_file="gateway_20260618.jsonl")

    assert ep["_key"] == "1a5d5635-0003"
    assert ep["session_id"] == "1a5d5635"
    assert ep["cycle"] == 3
    assert ep["user_message"] == "and manufactured silence?"
    assert ep["response"] == "You said attribution corruption is a kind of silence."
    assert ep["experiment_label"] == "claude_code"
    assert ep["source_file"] == "gateway_20260618.jsonl"
    # full prior context retained for provenance (premature-collapse: store everything)
    assert len(ep["messages_full"]) == 3


def test_ingest_gateway_file_loads_exchanges(tmp_path):
    """End-to-end: a pichay gateway log -> episodes. Only `request_metrics` events
    become episodes; others are skipped. Per-session sequence numbers key them."""
    db = get_database()
    ensure_index(db)
    col = db.collection(EPISODES)
    keys = ["sess9-0000", "sess9-0001"]
    try:
        events = [
            {"type": "anomaly", "kind": "ignore_me"},
            {"type": "request_metrics", "session_id": "sess9", "model": "claude-opus-4-8",
             "messages_full": [{"role": "user", "content": "first prompt"}],
             "response_text": "first reply"},
            {"type": "request_metrics", "session_id": "sess9", "model": "claude-opus-4-8",
             "messages_full": [{"role": "user", "content": "second prompt"}],
             "response_text": "second reply"},
        ]
        p = tmp_path / "gateway_sample.jsonl"
        p.write_text("\n".join(json.dumps(e) for e in events))

        n = ingest_gateway_file(db, p)

        assert n == 2
        assert col.get("sess9-0000")["response"] == "first reply"
        assert col.get("sess9-0001")["user_message"] == "second prompt"
        assert col.get("sess9-0001")["cycle"] == 1
    finally:
        for k in keys:
            if col.has(k):
                col.delete(k)


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
