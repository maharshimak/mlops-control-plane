from mlops_cp.canary import CanarySnapshot, evaluate_canary
from mlops_cp.lineage import LineageEdge, LineageGraph, LineageNode


def test_good_canary_increases_traffic() -> None:
    production = CanarySnapshot(1000, 0.01, 200, 0.90)
    candidate = CanarySnapshot(200, 0.012, 220, 0.91)

    decision = evaluate_canary(production, candidate, 20)

    assert decision.action == "increase"
    assert decision.next_traffic_percent == 40


def test_bad_canary_rolls_back() -> None:
    production = CanarySnapshot(1000, 0.01, 200, 0.90)
    candidate = CanarySnapshot(200, 0.08, 800, 0.80)

    decision = evaluate_canary(production, candidate, 40)

    assert decision.action == "rollback"
    assert decision.next_traffic_percent == 0


def test_lineage_upstream() -> None:
    graph = LineageGraph()
    graph.add_node(LineageNode("dataset", "dataset", {}))
    graph.add_node(LineageNode("model", "model", {}))
    graph.add_node(LineageNode("deployment", "deployment", {}))
    graph.link(LineageEdge("dataset", "TRAINS", "model"))
    graph.link(LineageEdge("model", "DEPLOYS", "deployment"))

    assert graph.upstream("deployment") == {"dataset", "model"}
