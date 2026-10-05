import sqlite3

import pytest

from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.registry import ModelRegistry


def _registered_model() -> ModelVersion:
    return ModelVersion(
        name="fraud",
        version="1.0.0",
        artifact_uri="model://fraud/1.0.0",
        dataset_fingerprint="dataset-sha",
    )


def test_registry_round_trips_models_evaluations_and_stage(tmp_path) -> None:
    path = tmp_path / "registry.db"
    registry = ModelRegistry(str(path))
    registry.register(_registered_model())
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


def test_registration_and_history_commit_atomically(tmp_path) -> None:
    path = tmp_path / "registry.db"
    registry = ModelRegistry(str(path))

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_registration_history
            BEFORE INSERT ON lifecycle_events
            WHEN NEW.to_stage = 'registered'
            BEGIN
                SELECT RAISE(ABORT, 'history unavailable');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="history unavailable"):
        registry.register(_registered_model())

    restarted = ModelRegistry(str(path))
    with pytest.raises(KeyError):
        restarted.get("fraud", "1.0.0")


def test_candidate_transition_and_history_commit_atomically(tmp_path) -> None:
    path = tmp_path / "registry.db"
    registry = ModelRegistry(str(path))
    registry.register(_registered_model())
    registry.add_evaluation(
        "fraud",
        "1.0.0",
        Evaluation("roc_auc", 0.92, 0.85),
    )

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_candidate_history
            BEFORE INSERT ON lifecycle_events
            WHEN NEW.to_stage = 'candidate'
            BEGIN
                SELECT RAISE(ABORT, 'history unavailable');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="history unavailable"):
        registry.promote_candidate("fraud", "1.0.0")

    restarted = ModelRegistry(str(path))
    assert restarted.get("fraud", "1.0.0").stage == "registered"
    assert [event.to_stage for event in restarted.history("fraud", "1.0.0")] == [
        "registered"
    ]
