import sqlite3
from contextlib import closing

import pytest
from conftest import ARTIFACT_BYTES, evidence_payload

from mlops_cp.artifacts import ArtifactAccessError
from mlops_cp.evidence import CandidateEvidence
from mlops_cp.governed import (
    DriftObservation,
    GovernanceError,
    GovernanceService,
    PromotionPolicyConfig,
)
from mlops_cp.models import ModelVersion

STABLE = DriftObservation(feature="amount", expected=[50, 30, 20], actual=[49, 31, 20])
CRITICAL = DriftObservation(feature="country", expected=[90, 10], actual=[20, 80])
WARNING = DriftObservation(feature="age", expected=[50, 50], actual=[30, 70])  # PSI ~0.169


def _service(tmp_path=None, **policy) -> GovernanceService:
    path = None if tmp_path is None else str(tmp_path / "cp.db")
    return GovernanceService(database_path=path, policy=PromotionPolicyConfig(**policy))


def _registered(service: GovernanceService, **overrides) -> CandidateEvidence:
    evidence = CandidateEvidence.model_validate(evidence_payload(**overrides))
    service.register_candidate(evidence)
    return evidence


def test_full_governed_path_records_an_intact_audit_trail(tmp_path) -> None:
    service = _service(tmp_path)
    _registered(service)

    assert service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES).verified
    pending = service.evaluate_promotion("fraud", "1.0.0", drift=[STABLE])
    assert pending.decision == "requires_approval"
    assert pending.reasons == ("requires 1 approval(s), has 0",)

    service.record_approval("fraud", "1.0.0", "risk-officer", "reviewed synthetic report")
    decision = service.promote_to_production("fraud", "1.0.0", drift=[STABLE])

    assert decision.decision == "allow" and decision.reasons == ()
    assert all(decision.checks.values())
    assert service.registry.get("fraud", "1.0.0").stage == "production"
    events = service.audit.events("fraud:1.0.0")
    assert [e.event_type for e in events] == [
        "candidate_registered",
        "artifact_verification",
        "approval_recorded",
        "promoted_to_production",
    ]
    assert service.audit.verify().valid
    # Existing lifecycle history is still written by the registry.
    assert [e.to_stage for e in service.registry.history("fraud", "1.0.0")] == [
        "registered", "candidate", "production"
    ]


def test_every_failing_reason_is_reported_at_once() -> None:
    service = _service()
    _registered(
        service,
        metrics=[{"name": "roc_auc", "value": 0.70, "threshold": 0.85}],
    )
    decision = service.evaluate_promotion("fraud", "1.0.0", drift=[CRITICAL])

    assert decision.decision == "deny"
    joined = " | ".join(decision.reasons)
    assert "artifact has not been verified" in joined
    assert "failed metrics: roc_auc" in joined
    assert "critical drift on feature 'country'" in joined
    assert "requires 1 approval(s), has 0" in joined
    assert decision.checks["artifact_verified"] is False
    assert decision.checks["evaluation_gates"] is False
    assert decision.checks["drift_within_policy"] is False
    assert decision.checks["approvals"] is False


def test_artifact_digest_mismatch_blocks_promotion_even_with_approval() -> None:
    service = _service()
    _registered(service)
    service.record_approval("fraud", "1.0.0", "risk-officer")
    result = service.verify_artifact("fraud", "1.0.0", content=b"tampered weights")
    assert not result.verified

    decision = service.evaluate_promotion("fraud", "1.0.0")
    assert decision.decision == "deny"
    assert "latest artifact verification failed" in decision.reasons


def test_a_later_failed_verification_overrides_an_earlier_success() -> None:
    service = _service()
    _registered(service)
    service.record_approval("fraud", "1.0.0", "risk-officer")
    service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    assert service.evaluate_promotion("fraud", "1.0.0").decision == "allow"
    service.verify_artifact("fraud", "1.0.0", content=b"changed later")
    assert service.evaluate_promotion("fraud", "1.0.0").decision == "deny"


def test_denied_promotion_does_not_change_registry_state() -> None:
    service = _service()
    _registered(service)
    decision = service.promote_to_production("fraud", "1.0.0")
    assert decision.decision == "deny"
    assert service.registry.get("fraud", "1.0.0").stage == "registered"
    assert "promoted_to_production" not in {e.event_type for e in service.audit.events()}


def test_model_without_evidence_fails_closed() -> None:
    service = _service(required_approvals=0)
    service.registry.register(ModelVersion("legacy", "1", "s3://synthetic/legacy", "abc"))
    decision = service.evaluate_promotion("legacy", "1")
    assert decision.decision == "deny"
    assert "no candidate evidence recorded" in decision.reasons


def test_separation_of_duties_and_duplicate_approvals() -> None:
    service = _service(required_approvals=2)
    _registered(service)
    with pytest.raises(GovernanceError, match="separation of duties"):
        service.record_approval("fraud", "1.0.0", "pipeline-bot")
    service.record_approval("fraud", "1.0.0", "alice")
    with pytest.raises(GovernanceError) as duplicate:
        service.record_approval("fraud", "1.0.0", "alice")
    assert duplicate.value.code == "CONFLICT"
    service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    assert service.evaluate_promotion("fraud", "1.0.0").decision == "requires_approval"
    service.record_approval("fraud", "1.0.0", "bob")
    assert service.evaluate_promotion("fraud", "1.0.0").decision == "allow"


def test_required_metrics_and_drift_policy_are_server_side() -> None:
    service = _service(required_approvals=0, required_metrics=("roc_auc", "calibration_ece"),
                       require_drift_evidence=True)
    _registered(service)
    service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    decision = service.evaluate_promotion("fraud", "1.0.0")
    assert decision.decision == "deny"
    assert "missing required metrics: calibration_ece" in decision.reasons
    assert "drift evidence is required but none was supplied" in decision.reasons


def test_warning_drift_is_reported_without_denying_by_default() -> None:
    service = _service(required_approvals=0)
    _registered(service)
    service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    decision = service.evaluate_promotion("fraud", "1.0.0", drift=[WARNING])
    assert decision.drift[0]["severity"] == "warning"
    assert decision.decision == "allow"
    assert any("warning drift" in item for item in decision.warnings)

    strict = _service(required_approvals=0, deny_drift_severity="warning")
    _registered(strict)
    strict.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    assert strict.evaluate_promotion("fraud", "1.0.0", drift=[WARNING]).decision == "deny"


def test_production_or_archived_models_cannot_be_promoted_again() -> None:
    service = _service(required_approvals=0)
    _registered(service)
    service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    assert service.promote_to_production("fraud", "1.0.0").decision == "allow"
    again = service.evaluate_promotion("fraud", "1.0.0")
    assert again.decision == "deny"
    assert "model stage 'production' cannot be promoted to production" in again.reasons


def test_duplicate_registration_is_a_conflict() -> None:
    service = _service()
    _registered(service)
    with pytest.raises(GovernanceError) as error:
        _registered(service)
    assert error.value.code == "CONFLICT"


def test_state_survives_restart_and_tables_are_insert_only(tmp_path) -> None:
    service = _service(tmp_path)
    _registered(service)
    service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    service.record_approval("fraud", "1.0.0", "risk-officer")

    restarted = _service(tmp_path)
    assert restarted.evaluate_promotion("fraud", "1.0.0").decision == "allow"

    with closing(sqlite3.connect(str(tmp_path / "cp.db"))) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="insert-only"):
            connection.execute("DELETE FROM approvals")
        with pytest.raises(sqlite3.IntegrityError, match="insert-only"):
            connection.execute("UPDATE candidate_evidence SET evidence_sha256 = 'x'")


def test_tampered_audit_log_denies_promotion(tmp_path) -> None:
    service = _service(tmp_path)
    _registered(service)
    service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    service.record_approval("fraud", "1.0.0", "risk-officer")
    with closing(sqlite3.connect(str(tmp_path / "cp.db"))) as connection, connection:
        connection.execute("DROP TRIGGER audit_events_no_update")
        # Rewrite the artifact_verification event (seq 2) behind the trigger.
        connection.execute(
            "UPDATE audit_events SET created_at = '2000-01-01T00:00:00+00:00' WHERE seq = 2"
        )

    decision = service.evaluate_promotion("fraud", "1.0.0")
    assert decision.decision == "deny"
    assert decision.checks["audit_chain_intact"] is False
    assert any(reason.startswith("audit log integrity failure at seq 2") for reason in
               decision.reasons)


def test_path_verification_is_confined_to_the_artifact_root(tmp_path) -> None:
    root = tmp_path / "artifacts"
    (root / "fraud").mkdir(parents=True)
    (root / "fraud" / "model.bin").write_bytes(ARTIFACT_BYTES)
    (tmp_path / "outside.bin").write_bytes(ARTIFACT_BYTES)

    no_root = _service()
    _registered(no_root)
    with pytest.raises(PermissionError):
        no_root.verify_artifact("fraud", "1.0.0", path="fraud/model.bin")

    service = GovernanceService(artifact_root=str(root))
    _registered(service)
    assert service.verify_artifact("fraud", "1.0.0", path="fraud/model.bin").verified
    for escape in ("../outside.bin", str(tmp_path / "outside.bin"), "fraud/missing.bin"):
        with pytest.raises(ArtifactAccessError):
            service.verify_artifact("fraud", "1.0.0", path=escape)


def test_policy_configuration_is_validated() -> None:
    with pytest.raises(ValueError):
        PromotionPolicyConfig(required_approvals=-1)
    with pytest.raises(TypeError):
        PromotionPolicyConfig(required_approvals=True)
    with pytest.raises(ValueError):
        PromotionPolicyConfig(deny_drift_severity="stable")
