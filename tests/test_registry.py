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


def test_concurrent_evaluation_appends_preserve_all_metrics(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db = tmp_path / "registry.db"
    primary = ModelRegistry(str(db))
    primary.register(ModelVersion(
        name="fraud", version="1", artifact_uri="model://fraud/1",
        dataset_fingerprint="data",
    ))
    writers = [ModelRegistry(str(db)) for _ in range(8)]

    def record(index):
        writers[index].add_evaluation(
            "fraud", "1",
            Evaluation(metric=f"metric_{index}", value=0.95, threshold=0.80),
        )

    with ThreadPoolExecutor(max_workers=len(writers)) as pool:
        list(pool.map(record, range(len(writers))))

    result = primary.get("fraud", "1")
    assert len(result.evaluations) == len(writers)
    assert {entry.metric for entry in result.evaluations} == {
        f"metric_{i}" for i in range(len(writers))
    }


def test_append_after_external_promotion_does_not_revert_stage(tmp_path):
    db = tmp_path / "registry.db"
    source = ModelRegistry(str(db))
    source.register(ModelVersion(
        name="fraud", version="1", artifact_uri="model://fraud/1",
        dataset_fingerprint="data",
    ))
    stale = ModelRegistry(str(db))
    source.add_evaluation(
        "fraud", "1", Evaluation(metric="auc", value=0.95, threshold=0.80),
    )
    source.promote_candidate("fraud", "1")
    source.promote_production("fraud", "1")

    stale.add_evaluation(
        "fraud", "1", Evaluation(metric="latency", value=10, threshold=15,
                                 higher_is_better=False),
    )
    reloaded = ModelRegistry(str(db))
    actual = reloaded.get("fraud", "1")
    assert actual.stage == "production"
    assert len(actual.evaluations) == 2


def test_evaluation_write_rolls_back_on_database_failure(tmp_path):
    import sqlite3

    import pytest

    db = tmp_path / "registry.db"
    registry = ModelRegistry(str(db))
    registry.register(ModelVersion(
        name="fraud", version="1", artifact_uri="model://fraud/1",
        dataset_fingerprint="data",
    ))
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TRIGGER prevent_evaluation BEFORE UPDATE OF evaluations_json "
            "ON model_versions BEGIN SELECT RAISE(ABORT, 'evaluation unavailable'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="evaluation unavailable"):
        registry.add_evaluation(
            "fraud", "1", Evaluation(metric="auc", value=0.95, threshold=0.80),
        )
    assert registry.get("fraud", "1").evaluations == []


def test_candidate_rechecks_latest_metrics_inside_write_transaction(tmp_path, monkeypatch):
    """A disqualifying evaluation arriving after the initial read must block promotion."""
    db = tmp_path / "racing-promotions.sqlite"
    first = ModelRegistry(str(db))
    second = ModelRegistry(str(db))
    first.register(ModelVersion(
        name="risk", version="1", artifact_uri="model://risk/1",
        dataset_fingerprint="dataset",
    ))
    first.add_evaluation(
        "risk", "1", Evaluation(metric="auc", value=0.96, threshold=0.80),
    )
    persist = first._persist_candidate_with_event

    def append_before_lock(model, event):
        second.add_evaluation(
            "risk", "1",
            Evaluation(metric="mae", value=5.0, threshold=2.0,
                       higher_is_better=False),
        )
        return persist(model, event)

    monkeypatch.setattr(first, "_persist_candidate_with_event", append_before_lock)
    decision = first.promote_candidate("risk", "1")
    assert not decision.allowed
    assert any("mae" in reason for reason in decision.reasons)
    fresh = ModelRegistry(str(db))
    assert fresh.get("risk", "1").stage == "registered"
    assert len(fresh.get("risk", "1").evaluations) == 2
    assert [event.to_stage for event in fresh.history("risk", "1")] == ["registered"]


def test_candidate_still_promotes_when_snapshot_has_valid_metrics(tmp_path):
    db = tmp_path / "valid-promotion.sqlite"
    registry = ModelRegistry(str(db))
    registry.register(ModelVersion(
        name="quality", version="1", artifact_uri="model://quality/1",
        dataset_fingerprint="dataset",
    ))
    registry.add_evaluation(
        "quality", "1", Evaluation(metric="auc", value=0.94, threshold=0.80),
    )
    assert registry.promote_candidate("quality", "1").allowed
    assert registry.get("quality", "1").stage == "candidate"
    assert [event.to_stage for event in registry.history("quality", "1")] == [
        "registered", "candidate",
    ]


def test_production_rechecks_metrics_after_concurrent_append(tmp_path, monkeypatch):
    import pytest

    import mlops_cp.registry as registry_module

    db = tmp_path / "production-race.sqlite"
    candidate = ModelRegistry(str(db))
    external = ModelRegistry(str(db))
    candidate.register(ModelVersion(
        name="quality", version="1", artifact_uri="model://quality",
        dataset_fingerprint="dataset",
    ))
    candidate.add_evaluation(
        "quality", "1", Evaluation(metric="auc", value=0.96, threshold=0.8),
    )
    assert candidate.promote_candidate("quality", "1").allowed

    evaluate = registry_module.evaluate_promotion
    injected = False

    def add_failing_evaluation_once(model, *args, **kwargs):
        nonlocal injected
        decision = evaluate(model, *args, **kwargs)
        if not injected and model.stage == "candidate":
            injected = True
            external.add_evaluation(
                "quality", "1",
                Evaluation(metric="mae", value=20, threshold=2, higher_is_better=False),
            )
        return decision

    monkeypatch.setattr(registry_module, "evaluate_promotion", add_failing_evaluation_once)
    with pytest.raises(ValueError, match="Current evaluations"):
        candidate.promote_production("quality", "1")
    fresh = ModelRegistry(str(db))
    assert fresh.get("quality", "1").stage == "candidate"
    assert len(fresh.get("quality", "1").evaluations) == 2
    assert [event.to_stage for event in fresh.history("quality", "1")] == [
        "registered", "candidate",
    ]


def test_production_rejects_stale_archival_plan(tmp_path, monkeypatch):
    import pytest

    import mlops_cp.registry as registry_module

    db = tmp_path / "competing-promotion.sqlite"
    first = ModelRegistry(str(db))
    other = ModelRegistry(str(db))
    for version in ("1", "2"):
        first.register(ModelVersion(
            name="risk", version=version, artifact_uri=f"model://risk/{version}",
            dataset_fingerprint="dataset",
        ))
        first.add_evaluation(
            "risk", version, Evaluation(metric="auc", value=0.96, threshold=0.8),
        )
        first.promote_candidate("risk", version)

    # The first promotion completes in a different process only after the
    # competitor has read the list of existing production models.
    evaluate = registry_module.evaluate_promotion
    injected = False

    def competing_promotion(model, *args, **kwargs):
        nonlocal injected
        decision = evaluate(model, *args, **kwargs)
        if model.name == "risk" and model.version == "2" and not injected:
            injected = True
            other.promote_production("risk", "1")
        return decision

    monkeypatch.setattr(registry_module, "evaluate_promotion", competing_promotion)
    with pytest.raises(RuntimeError, match="Concurrent production promotion"):
        first.promote_production("risk", "2")
    final = ModelRegistry(str(db))
    assert final.get("risk", "1").stage == "production"
    assert final.get("risk", "2").stage == "candidate"
    assert len(final.history("risk", "2")) == 2


def test_concurrent_production_promotion_of_same_model_is_single_writer(tmp_path):
    """Only one independent worker may record the production transition."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db = tmp_path / "stress-promotion.sqlite"
    setup = ModelRegistry(str(db))
    setup.register(ModelVersion(
        name="risk", version="v1", artifact_uri="model://risk/v1",
        dataset_fingerprint="data",
    ))
    setup.add_evaluation(
        "risk", "v1", Evaluation(metric="auc", value=0.98, threshold=0.85),
    )
    assert setup.promote_candidate("risk", "v1").allowed

    workers = [ModelRegistry(str(db)) for _ in range(8)]
    ready = Barrier(len(workers))

    def compete(worker):
        ready.wait(timeout=10)
        try:
            worker.promote_production("risk", "v1")
            return "promoted"
        except (ValueError, RuntimeError):
            return "rejected"

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        outcomes = list(pool.map(compete, workers))
    assert outcomes.count("promoted") == 1
    assert outcomes.count("rejected") == 7
    verify = ModelRegistry(str(db))
    assert verify.get("risk", "v1").stage == "production"
    assert [item.to_stage for item in verify.history("risk", "v1")] == [
        "registered", "candidate", "production",
    ]


def test_failed_promotion_audit_insert_rolls_back_old_and_new_stages(tmp_path):
    import sqlite3

    import pytest

    db = tmp_path / "rollback-production.sqlite"
    registry = ModelRegistry(str(db))
    for version in ("1", "2"):
        registry.register(ModelVersion(
            name="quality", version=version,
            artifact_uri=f"model://quality/{version}", dataset_fingerprint="data",
        ))
        registry.add_evaluation(
            "quality", version,
            Evaluation(metric="auc", value=0.95, threshold=0.8),
        )
        registry.promote_candidate("quality", version)
    registry.promote_production("quality", "1")
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TRIGGER fail_new_production_event "
            "BEFORE INSERT ON lifecycle_events "
            "WHEN NEW.to_stage = 'production' AND NEW.model_key = 'quality:2' "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )

    with pytest.raises(sqlite3.DatabaseError, match="audit unavailable"):
        registry.promote_production("quality", "2")
    verify = ModelRegistry(str(db))
    assert verify.get("quality", "1").stage == "production"
    assert verify.get("quality", "2").stage == "candidate"
    assert [event.to_stage for event in verify.history("quality", "2")] == [
        "registered", "candidate",
    ]
