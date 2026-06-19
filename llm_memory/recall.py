from llm_memory.index import EPISODES


def recall(db, key):
    """Return one episode IN FULL by its `_key`, or None if absent. This is the
    second half of the reach: `search` ranks and hands back a 200-char snippet
    plus the episode's `key`; `recall(db, key)` fetches that whole episode so the
    instance never has to leave the memory surface to read what it found. A pure
    point lookup — no view, no BM25."""
    return db.collection(EPISODES).get(key)
