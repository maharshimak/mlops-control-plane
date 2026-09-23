from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.registry import ModelRegistry


def test_registry_round_trips_models_evaluations_and_stage(tmp_path) -> None:
    path = tmp_path / "registry.db"
    registry = ModelRegistry(str(path))
    registry.register(
        ModelVersion(
            name="fraud",
            version="1.0.0",
            artifact_uri="model://fraud/1.0.0",
            dataset_fingerprint="dataset-sha",
        )
    )
    registry.add_evaluation(
        "fraud",
        "1.0.0",
        Evaluation("roc_auc", 0.92, 0.85),
    )
    assert registry.promote_candidate("fraud", "1.0.0").allowed
    registry.promote_production("fraud", "1.0.0")

    restarted = ModelRegistry(str(path))
    restored = restarted.get("fraud", "1.0.0")

    assert restored.stage == "production"
    assert restored.dataset_fingerprint == "dataset-sha"
    assert restored.evaluations == [Evaluation("roc_auc", 0.92, 0.85)]
