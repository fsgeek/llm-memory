from llm_memory.index import ANALYZER, VIEW

_AQL = """
FOR doc IN @@view
  SEARCH ANALYZER(
    doc.user_message IN TOKENS(@q, @analyzer) OR
    doc.response     IN TOKENS(@q, @analyzer) OR
    doc.state_text   IN TOKENS(@q, @analyzer),
    @analyzer)
  OPTIONS { waitForSync: true }
  __SCOPE_FILTER__
  __SINCE_FILTER__
  __UNTIL_FILTER__
  LET score = BM25(doc)
  SORT score DESC
  LIMIT @limit
  RETURN { _key: doc._key, cycle: doc.cycle, score: score,
           ts: doc.ts, experiment_label: doc.experiment_label,
           source_file: doc.source_file,
           user_message: doc.user_message, response: doc.response,
           state_text: doc.state_text }
"""

_FIELDS = ("response", "user_message", "state_text")


def _matched_field(doc, query):
    """Attribute a hit to the field that best explains it. The search engine
    matched on a stemmed analyzer (text_en), so a literal substring check can
    miss a legitimate match (e.g. query "truncation" -> stem "truncat"). We
    therefore prefer the field with the most literal token overlap, but never
    return None for a doc the engine *did* match: if stemming hides every
    literal token, fall back to the longest non-empty field. This keeps
    matched_field/snippet honest about which prose carried the hit rather than
    silently blanking it (the failure that motivated this project)."""
    tokens = set(query.lower().split())
    best, best_overlap = None, 0
    for field in _FIELDS:
        text = (doc.get(field) or "").lower()
        overlap = sum(1 for t in tokens if t in text)
        if overlap > best_overlap:
            best, best_overlap = field, overlap
    if best is not None:
        return best
    # Stemmed-only match: the engine matched but no literal token survived.
    # Attribute to the longest field that actually has content.
    nonempty = [(len(doc.get(f) or ""), f) for f in _FIELDS if doc.get(f)]
    return max(nonempty)[1] if nonempty else None


def search(db, query, scope="all", limit=10, view=VIEW, since=None, until=None):
    """Search an ArangoSearch view with BM25 ranking. Defaults to the
    conversation-inclusive view (user_message + response + state_text); `view`
    can target another (e.g. a state-only view for controlled comparison).
    `scope` partitions by `experiment_label`: the default "all" searches every
    corpus, any other value restricts results to episodes with that label (e.g.
    "claude_code" so a live-session query cannot surface taste_open episodes).
    `since`/`until` bound the episode timestamp (ISO date or datetime strings,
    inclusive/exclusive). Returns {"total": N, "hits": [...]}: `total` is how
    many episodes matched before LIMIT, so a caller can see it is looking at
    ten of eight thousand and narrow, rather than mistake the page for the
    answer (amendment A7)."""
    bind_vars = {"@view": view, "q": query, "analyzer": ANALYZER, "limit": limit}
    aql = _AQL
    filters = {
        "__SCOPE_FILTER__": ("FILTER doc.experiment_label == @scope", "scope", None if scope == "all" else scope),
        "__SINCE_FILTER__": ("FILTER doc.ts >= @since", "since", since),
        "__UNTIL_FILTER__": ("FILTER doc.ts < @until", "until", until),
    }
    for marker, (clause, name, value) in filters.items():
        if value is None:
            aql = aql.replace(f"  {marker}\n", "")
        else:
            aql = aql.replace(marker, clause)
            bind_vars[name] = value
    cursor = db.aql.execute(aql, bind_vars=bind_vars, full_count=True)
    hits = []
    for doc in cursor:
        field = _matched_field(doc, query)
        snippet = (doc.get(field) or "")[:200] if field else ""
        hits.append(
            {
                "key": doc["_key"],
                "cycle": doc["cycle"],
                "score": doc["score"],
                # Provenance (amendment A1): a hit's date, project, and
                # source path are worth more than its snippet.
                "ts": doc.get("ts"),
                "experiment_label": doc.get("experiment_label"),
                "source_file": doc.get("source_file"),
                "matched_field": field,
                "snippet": snippet,
            }
        )
    return {"total": cursor.statistics()["fullCount"], "hits": hits}
