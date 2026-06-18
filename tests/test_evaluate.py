from llm_memory.evaluate import hit_at_k


def test_hit_at_k_true_when_expected_in_topk():
    assert hit_at_k([457, 456, 201], [455, 456], k=3) is True


def test_hit_at_k_false_when_expected_below_cutoff():
    assert hit_at_k([201, 98, 235, 444], [444], k=3) is False
