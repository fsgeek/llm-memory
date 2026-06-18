from llm_memory.db import get_database
from llm_memory.index import EPISODES, VIEW, ensure_index


def test_ensure_index_creates_collection_and_view():
    db = get_database()
    ensure_index(db)

    assert db.has_collection(EPISODES)

    view_names = [v["name"] for v in db.views()]
    assert VIEW in view_names

    # the view must link the conversational fields, not just state
    view = db.view(VIEW)
    fields = view["links"][EPISODES]["fields"]
    assert "user_message" in fields
    assert "response" in fields
    assert "state_text" in fields


def test_ensure_index_is_idempotent():
    db = get_database()
    ensure_index(db)
    ensure_index(db)  # must not raise on second call
    assert db.has_collection(EPISODES)
