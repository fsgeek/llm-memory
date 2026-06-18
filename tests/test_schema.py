from llm_memory.schema import flatten_state


def test_flatten_state_collects_nested_string_values():
    state = {
        "observation": "the drowning wall",
        "nested": {"reflection": "forty cycles", "items": [1, "context window"]},
    }
    text = flatten_state(state)
    assert isinstance(text, str)
    for expected in ["the drowning wall", "forty cycles", "context window", "1"]:
        assert expected in text
