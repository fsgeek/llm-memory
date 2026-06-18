import json

from llm_memory.index import EPISODES
from llm_memory.schema import flatten_state


def record_to_episode(record, source_file):
    """Transform one taste_open cycle record into an episode document that carries
    BOTH sides of the conversation and the flattened state."""
    state = record.get("state") or {}
    raw = record.get("raw_output")
    response = raw.get("response") if isinstance(raw, dict) else None
    if not response:
        response = record.get("response_text", "")

    cycle = record["cycle"]
    return {
        "_key": f"{cycle:06d}",
        "cycle": cycle,
        "ts": record.get("timestamp"),
        "model": record.get("model"),
        "experiment_label": record.get("experiment_label"),
        "source_file": source_file,
        "user_message": record.get("user_message", "") or "",
        "response": response or "",
        "state": state,
        # exclude _activity_log (tool-trace) — it echoes the instance's own
        # queries and would confound recall if indexed as state.
        "state_text": flatten_state(
            {k: v for k, v in state.items() if k != "_activity_log"}
        ),
        "activity_log": state.get("_activity_log", []),
    }


def ingest_file(db, path):
    """Load a taste_open jsonl into the episodes collection. Idempotent per cycle
    (overwrite by _key). Returns the number of records ingested."""
    col = db.collection(EPISODES)
    source = str(path)
    count = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            episode = record_to_episode(json.loads(line), source_file=source)
            col.insert(episode, overwrite=True)
            count += 1
    return count
