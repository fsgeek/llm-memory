"""How the store is used, kept in the store (amendment A23).

Every `search`, `recall`, and `describe` the server answers becomes one
document in the `queries` collection: the query and its window, how many
episodes matched, which keys came back at which rank and score, and how long
it took. A `recall` of a key that this session's earlier search returned is
linked to that search and rank — the nearest thing to a relevance judgment
the store can get without anyone labelling anything.

This is the workload that "what can we forget and still answer correctly?"
has to be measured against. It is not episode content: the collection is
deliberately not in the `episodes_search` view, so an instance's search never
finds its own past queries and mistakes them for evidence (the June
`_activity_log` confound). Records carry keys and scores, never hit text, and
at most MAX_HITS hits: a query record must stay small however wide the query.

Recording never fails a tool. A write that cannot happen is dropped with a
note on stderr, like the operational event log.
"""

import socket
import sys
import time
import uuid
from datetime import UTC, datetime

QUERIES = "queries"
MAX_HITS = 100


def _now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class QueryHistory:
    """One per server process. Under stdio a process serves one session, so
    a uuid made at start names the session; the searches it has answered are
    what a later `recall` can be linked to."""

    def __init__(self, get_db, project=None):
        self._get_db = get_db
        self.session = str(uuid.uuid4())
        self.project = project
        self.host = socket.gethostname()
        self._returned = {}  # episode key -> (query record key, rank), latest search wins
        self._ready = False

    def _insert(self, make_doc):
        """Insert the document `make_doc()` builds; building it is inside the
        guard too, so a result of an unexpected shape cannot fail the tool."""
        try:
            doc = make_doc()
            db = self._get_db()
            if not self._ready:
                if not db.has_collection(QUERIES):
                    db.create_collection(QUERIES)
                self._ready = True
            return db.collection(QUERIES).insert(
                {"ts": _now(), "session": self.session, "project": self.project, "host": self.host, **doc}
            )["_key"]
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not fail the tool
            try:
                sys.stderr.write(f"query history write failed: {type(exc).__name__}\n")
            except Exception:  # noqa: BLE001
                pass
            return None

    def search(self, *, query, scope, since, until, limit, result, elapsed):
        returned = []  # every key, for linking a later recall; only MAX_HITS are stored

        def make():
            returned.extend(h["key"] for h in result["hits"])
            hits = [
                {"key": h["key"], "rank": i, "score": h.get("score")}
                for i, h in enumerate(result["hits"][:MAX_HITS])
            ]
            return {
                "kind": "search",
                "query": query,
                "scope": scope,
                "since": since,
                "until": until,
                "limit": limit,
                "total": result["total"],
                "returned": len(result["hits"]),
                "hits": hits,
                "elapsed_ms": round(elapsed * 1000, 1),
            }

        key = self._insert(make)
        if key is not None:
            for rank, k in enumerate(returned):
                self._returned[k] = (key, rank)

    def recall(self, *, key, found):
        after, rank = self._returned.get(key, (None, None))
        self._insert(lambda: {"kind": "recall", "key": key, "found": found, "after_search": after, "rank": rank})

    def describe(self):
        self._insert(lambda: {"kind": "describe"})


def timed(fn, *args, **kwargs):
    """Call fn and return (result, seconds)."""
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, time.perf_counter() - start
