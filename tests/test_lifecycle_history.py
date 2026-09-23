from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.registry import ModelRegistry


def test_lifecycle_history_survives_restart(tmp_path) -> None:
    path = tmp_path / "registry.db"
    registry = ModelRegistry(str(path))
    registry.register(ModelVersion("risk", "1", "model://risk/1", "dataset-v1"))
    registry.add_evaluation("risk", "1", Evaluation("accuracy", 0.9, 0.8))
    assert registry.promote_candidate("risk", "1").allowed
    registry.promote_production("risk", "1")

    restarted = ModelRegistry(str(path))
    events = restarted.history("risk", "1")

    assert [(event.from_stage, event.to_stage) for event in events] == [
        (None, "registered"),
        ("registered", "candidate"),
        ("candidate", "production"),
    ]
    assert all(event.reason for event in events)
