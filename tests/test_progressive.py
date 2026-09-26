from mlops_cp.canary import CanarySnapshot
from mlops_cp.progressive import RolloutPolicy, evaluate_progressive_rollout


def snapshot(*, requests=200, errors=0.01, latency=100, quality=0.9):
    return CanarySnapshot(
        requests=requests,
        error_rate=errors,
        p95_latency_ms=latency,
        quality_score=quality,
    )


def test_progressive_rollout_advances_through_configured_stage():
    decision = evaluate_progressive_rollout(snapshot(), snapshot(), 5)
    assert decision.action == "increase"
    assert decision.next_traffic_percent == 25


def test_progressive_rollout_rolls_back_on_quality_regression():
    decision = evaluate_progressive_rollout(
        snapshot(quality=0.95),
        snapshot(quality=0.7),
        25,
        policy=RolloutPolicy(max_quality_drop=0.05),
    )
    assert decision.action == "rollback"
    assert decision.next_traffic_percent == 0
