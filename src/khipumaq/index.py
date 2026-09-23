EPISODES = "episodes"
VIEW = "episodes_search"
# The fix: index conversation (both sides) AND flattened state — not state alone.
INDEXED_FIELDS = ["user_message", "response", "state_text"]
ANALYZER = "text_en"  # built-in: tokenize, lowercase, stem, stopwords -> BM25


def _view_properties():
    return {
        "links": {
            EPISODES: {
                "fields": {f: {"analyzers": [ANALYZER]} for f in INDEXED_FIELDS}
            }
        }
    }


def ensure_index(db):
    """Idempotently ensure the episodes collection and the conversation-inclusive
    ArangoSearch view exist."""
    if not db.has_collection(EPISODES):
        db.create_collection(EPISODES)

    props = _view_properties()
    if VIEW in [v["name"] for v in db.views()]:
        db.update_arangosearch_view(VIEW, props)
    else:
        db.create_arangosearch_view(VIEW, properties=props)
