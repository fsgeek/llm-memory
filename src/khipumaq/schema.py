def flatten_state(state):
    """Recursively collect every scalar value in a nested structure into one
    searchable string. Used to build `state_text` so ArangoSearch can index the
    instance's self-curated state alongside the conversation."""
    parts = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif node is None:
            return
        else:
            parts.append(str(node))

    walk(state)
    return " ".join(parts)
