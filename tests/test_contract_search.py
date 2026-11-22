from capcov.contract_search import evaluate


def test_full_contract_disagreement_and_refinement():
    rows = [{"input":{"a":False,"b":False}, "output":False},
            {"input":{"a":True,"b":True}, "output":True}]
    pool = [{"id":"reachable", "input":{"a":True,"b":False}}]
    task = {"id":"target","examples":rows, "reachable_inputs":pool}
    limits = {"max_terms":100, "max_cost":5, "max_probes":1}
    first = evaluate(task, {}, **limits)
    assert first["known_examples"] == 2
    assert first["probes"][0]["outcome"] == "disagreement"
    rows.append({"input":pool[0]["input"],"output":True})
    refined = evaluate(task, {}, **limits)
    assert refined["known_examples"] == 3
    assert refined["probes"][0]["outcome"] == "covered_feature_vector"
