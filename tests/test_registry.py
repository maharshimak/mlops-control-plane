from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.registry import ModelRegistry


def test_quality_gate_and_production_promotion() -> None:
    registry = ModelRegistry()
    registry.register(
        ModelVersion(
            name="churn",
            version="1.0.0",
            artifact_uri="s3://models/churn/1.0.0",
            dataset_fingerprint="abc",
        )
    )
    registry.add_evaluation(
        "churn",
        "1.0.0",
        Evaluation(
            metric="roc_auc",
            value=0.91,
            threshold=0.85,
        ),
    )

    decision = registry.promote_candidate("churn", "1.0.0")
    assert decision.allowed

    registry.promote_production("churn", "1.0.0")
    assert registry.get("churn", "1.0.0").stage == "production"


def test_failed_metric_blocks_candidate() -> None:
    registry = ModelRegistry()
    registry.register(
        ModelVersion(
            name="risk",
            version="2",
            artifact_uri="model://risk/2",
            dataset_fingerprint="xyz",
        )
    )
    registry.add_evaluation(
        "risk",
        "2",
        Evaluation(
            metric="mae",
            value=5.0,
            threshold=2.0,
            higher_is_better=False,
        ),
    )
    assert not registry.promote_candidate("risk", "2").allowed
