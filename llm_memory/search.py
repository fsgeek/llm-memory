from llm_memory.index import ANALYZER, VIEW

_AQL = """
FOR doc IN @@view
  SEARCH ANALYZER(
    doc.user_message IN TOKENS(@q, @analyzer) OR
    doc.response     IN TOKENS(@q, @analyzer) OR
    doc.state_text   IN TOKENS(@q, @analyzer),
    @analyzer)
  OPTIONS { waitForSync: true }
  LET score = BM25(doc)
  SORT score DESC
  LIMIT @limit
  RETURN { cycle: doc.cycle, score: score,
           user_message: doc.user_message, response: doc.response }
"""


def _matched_field(doc, query):
    tokens = set(query.lower().split())
    best, best_overlap = None, 0
    for field in ("response", "user_message"):
        text = (doc.get(field) or "").lower()
        overlap = sum(1 for t in tokens if t in text)
        if overlap > best_overlap:
            best, best_overlap = field, overlap
    return best


def search(db, query, scope="all", limit=10, view=VIEW):
    """Search an ArangoSearch view with BM25 ranking. Defaults to the
    conversation-inclusive view (user_message + response + state_text); `view`
    can target another (e.g. a state-only view for controlled comparison).
    `scope` is accepted for parity with the existing search_memory API; in the
    single-corpus sandbox it does not yet partition results."""
    cursor = db.aql.execute(
        _AQL,
        bind_vars={"@view": view, "q": query, "analyzer": ANALYZER, "limit": limit},
    )
    hits = []
    for doc in cursor:
        field = _matched_field(doc, query)
        snippet = (doc.get(field) or "")[:200] if field else ""
        hits.append(
            {
                "cycle": doc["cycle"],
                "score": doc["score"],
                "matched_field": field,
                "snippet": snippet,
            }
        )
    return hits
