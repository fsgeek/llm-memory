def hit_at_k(result_cycles, expected_cycles, k=3):
    """True if any expected cycle appears within the top-k result cycles."""
    expected = set(expected_cycles)
    return any(cycle in expected for cycle in result_cycles[:k])
