from itertools import product

from capcov.recognition import for_examples, sleep
from capcov.boolean_search import search
from capcov.synthesis import eval_program


def examples(fn):
    return [
        {"input": {"alpha": a, "beta": b}, "output": fn(a, b)}
        for a, b in product((False, True), repeat=2)
    ]


def test_sampled_programs_train_transferable_production_prior():
    library = {
        "invert": {"params": ["p"], "body": {"op": "not", "args": [{"var": "p"}]}}
    }
    policy = sleep([], library, dreams=100)
    assert policy == sleep([], library, dreams=100)
    assert policy["synthetic_tasks"] == 100 and policy["synthetic_examples"] <= 1600
    assert any(row["production"] == "invert" for row in policy["prototypes"])
    for name, fn in [("and", lambda a, b: a and b), ("or", lambda a, b: a or b)]:
        rows = examples(fn)
        votes = for_examples(policy, rows)
        assert max(votes, key=votes.get) == name
        assert votes == for_examples(policy, rows[::-1])
        run = search(rows, library, max_terms=200, max_cost=4, policy=policy)
        assert all(
            eval_program(run["program"], r["input"], library) == r["output"]
            for r in rows
        )
