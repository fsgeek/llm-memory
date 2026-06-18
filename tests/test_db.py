from llm_memory.db import get_database


def test_get_database_connects_as_scoped_user():
    db = get_database()
    assert db.name == "llm_memory"
    # proves auth + access actually work (not just object construction)
    assert isinstance(db.collections(), list)
