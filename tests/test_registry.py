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


def test_failed_registration_does_not_change_in_memory_registry(monkeypatch):
    import pytest

    registry = ModelRegistry()
    model = ModelVersion(
        name="unavailable",
        version="1",
        artifact_uri="model://unavailable/1",
        dataset_fingerprint="abc",
    )

    def fail_to_persist(model):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(registry, "_persist", fail_to_persist)
    with pytest.raises(OSError, match="storage failure"):
        registry.register(model)
    assert registry.list() == []


def test_failed_evaluation_persistence_preserves_previous_state(monkeypatch):
    import pytest

    registry = ModelRegistry()
    model = registry.register(
        ModelVersion(
            name="durable",
            version="1",
            artifact_uri="model://durable/1",
            dataset_fingerprint="abc",
        )
    )

    def fail_to_persist(updated):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(registry, "_persist", fail_to_persist)
    with pytest.raises(OSError, match="storage failure"):
        registry.add_evaluation(
            "durable",
            "1",
            Evaluation(metric="mae", value=0.2, threshold=0.5, higher_is_better=False),
        )
    assert model.evaluations == []
    assert registry.get("durable", "1").evaluations == []


def test_registration_audit_failure_rolls_back_both_writes(tmp_path):
    import sqlite3

    import pytest

    db = tmp_path / "registry.db"
    registry = ModelRegistry(str(db))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON lifecycle_events "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
    model = ModelVersion(
        name="risk", version="1", artifact_uri="model://risk/1",
        dataset_fingerprint="test",
    )
    with pytest.raises(sqlite3.DatabaseError, match="audit unavailable"):
        registry.register(model)

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_versions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == 0
    assert registry.list() == []


def test_candidate_audit_failure_preserves_registered_stage(tmp_path):
    import sqlite3

    import pytest

    db = tmp_path / "registry.db"
    registry = ModelRegistry(str(db))
    registry.register(ModelVersion(
        name="risk", version="1", artifact_uri="model://risk/1",
        dataset_fingerprint="test",
    ))
    registry.add_evaluation(
        "risk", "1", Evaluation(metric="roc_auc", value=0.95, threshold=0.80),
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TRIGGER fail_candidate_audit BEFORE INSERT ON lifecycle_events "
            "WHEN NEW.to_stage = 'candidate' "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )

    with pytest.raises(sqlite3.DatabaseError, match="audit unavailable"):
        registry.promote_candidate("risk", "1")
    assert registry.get("risk", "1").stage == "registered"
    assert len(registry.history("risk", "1")) == 1


def test_duplicate_registration_race_never_overwrites_existing_row(tmp_path):
    import pytest

    db = tmp_path / "registry.db"
    first = ModelRegistry(str(db))
    second = ModelRegistry(str(db))
    existing = ModelVersion(
        name="same", version="1", artifact_uri="model://original",
        dataset_fingerprint="original",
    )
    first.register(existing)
    with pytest.raises(ValueError, match="already exists"):
        second.register(ModelVersion(
            name="same", version="1", artifact_uri="model://replacement",
            dataset_fingerprint="replacement",
        ))
    assert first.get("same", "1").artifact_uri == "model://original"
    assert len(first.history("same", "1")) == 1
