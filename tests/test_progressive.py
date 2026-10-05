from mlops_cp.canary import CanarySnapshot
from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.progressive import (
    RolloutPolicy,
    evaluate_governed_rollout,
    evaluate_progressive_rollout,
)


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


def model(version: str, accuracy: float, latency: float, stage: str) -> ModelVersion:
    return ModelVersion(
        name="fraud",
        version=version,
        artifact_uri=f"s3://models/fraud/{version}",
        dataset_fingerprint="dataset-v1",
        stage=stage,
        evaluations=[
            Evaluation("accuracy", accuracy, 0.90, True),
            Evaluation("latency", latency, 150.0, False),
        ],
    )


def test_governed_rollout_rolls_back_when_offline_metrics_regress() -> None:
    decision = evaluate_governed_rollout(
        snapshot(),
        snapshot(),
        25,
        baseline_model=model("1", 0.96, 90, "production"),
        candidate_model=model("2", 0.91, 140, "candidate"),
        regression_tolerance=0.01,
    )

    assert decision.action == "rollback"
    assert decision.next_traffic_percent == 0
    assert any("offline regression" in reason for reason in decision.reasons)
