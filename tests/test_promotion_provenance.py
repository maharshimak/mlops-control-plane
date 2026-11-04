import sqlite3
from contextlib import closing

import pytest
from conftest import ARTIFACT_BYTES, evidence_payload
from fastapi.testclient import TestClient

from mlops_cp import api
from mlops_cp.evidence import CandidateEvidence
from mlops_cp.governed import GovernanceService, PromotionPolicyConfig
from mlops_cp.models import ModelVersion


def _service(path, *, trusted=True, require_verification=True):
    return GovernanceService(
        database_path=str(path),
        trusted_inputs=trusted,
        policy=PromotionPolicyConfig(
            required_approvals=0, require_artifact_verification=require_verification
        ),
    )


def _register(service):
    service.register_candidate(CandidateEvidence.model_validate(evidence_payload()))


def _verify(service):
    assert service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES).verified


def _assert_eligibility(service, expected):
    preview = service.evaluate_promotion("fraud", "1.0.0")
    assert preview.decision == expected
    assert service.promote_to_production("fraud", "1.0.0") == preview
    assert service.registry.get("fraud", "1.0.0").stage == (
        "production" if expected == "allow" else "registered"
    )
    return preview


@pytest.mark.parametrize("trusted_reader", [False, True])
def test_untrusted_registration_is_ineligible_with_zero_approvals(tmp_path, trusted_reader):
    path = tmp_path / "registry.db"
    _register(_service(path, trusted=False))
    reader = _service(path, trusted=trusted_reader)
    _verify(_service(path))
    decision = _assert_eligibility(reader, "deny")
    assert decision.checks["trusted_registration"] is False
    assert decision.checks["trusted_artifact_verification"] is True


def test_latest_untrusted_artifact_check_supersedes_trusted_verification(tmp_path):
    path = tmp_path / "registry.db"
    service = _service(path)
    _register(service)
    _verify(service)
    _verify(_service(path, trusted=False))
    decision = _assert_eligibility(service, "deny")
    assert decision.checks["trusted_registration"] is True
    assert decision.checks["artifact_verified"] is True
    assert decision.checks["trusted_artifact_verification"] is False


def test_http_registration_cannot_be_promoted_by_a_trusted_service(tmp_path, monkeypatch):
    path = tmp_path / "registry.db"
    monkeypatch.setattr(api, "governance", _service(path, trusted=False))
    monkeypatch.setenv("MLOPS_CP_API_TOKEN", "synthetic-key")
    response = TestClient(api.app).post(
        "/v1/ops/register_candidate",
        json=evidence_payload(),
        headers={"Authorization": "Bearer synthetic-key", "X-Principal": "trusted"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    trusted = _service(path)
    _verify(trusted)
    assert not _assert_eligibility(trusted, "deny").checks["trusted_registration"]


def test_untrusted_service_cannot_preview_an_allow_for_trusted_inputs(tmp_path):
    path = tmp_path / "registry.db"
    service = _service(path)
    _register(service)
    _verify(service)
    decision = _assert_eligibility(_service(path, trusted=False), "deny")
    assert decision.checks["trusted_promotion_service"] is False
    assert decision.checks["trusted_registration"] is True


@pytest.mark.parametrize("require_verification", [False, True])
def test_trusted_preview_and_commit_agree(tmp_path, require_verification):
    service = _service(tmp_path / "registry.db", require_verification=require_verification)
    _register(service)
    if require_verification:
        _verify(service)
    decision = _assert_eligibility(service, "allow")
    assert decision.checks["trusted_promotion_inputs"] is True


def test_candidate_without_provenance_metadata_is_ineligible(tmp_path):
    path = tmp_path / "registry.db"
    service = _service(path)
    evidence = CandidateEvidence.model_validate(evidence_payload())
    service.registry.register(
        ModelVersion(
            evidence.model_name,
            evidence.model_version,
            evidence.artifact_uri,
            evidence.dataset_fingerprint,
            evaluations=[metric.to_evaluation() for metric in evidence.metrics],
        )
    )
    service._store_evidence(evidence, evidence.sha256())
    service.audit.append(
        "candidate_registered",
        evidence.model_key,
        {
            "evidence_sha256": evidence.sha256(),
        },
    )
    _verify(service)
    decision = _assert_eligibility(service, "deny")
    assert decision.checks["evidence_digest_verified"] is True
    assert decision.checks["trusted_registration"] is False


@pytest.mark.parametrize("invalid", [None, "not json", "{}"])
def test_missing_and_malformed_stored_evidence_is_denied(tmp_path, invalid):
    path = tmp_path / "registry.db"
    service = _service(path)
    _register(service)
    _verify(service)
    with closing(sqlite3.connect(path)) as conn, conn:
        if invalid is None:
            conn.execute("DROP TRIGGER candidate_evidence_no_delete")
            conn.execute("DELETE FROM candidate_evidence")
        else:
            conn.execute("DROP TRIGGER candidate_evidence_no_update")
            conn.execute("UPDATE candidate_evidence SET evidence_json=?", (invalid,))
    assert _assert_eligibility(service, "deny").checks["evidence_recorded"] is False


@pytest.mark.parametrize("payload", ["not json", "[]", '{"invalid": NaN}'])
def test_malformed_audit_payload_is_denied(tmp_path, payload):
    path = tmp_path / "registry.db"
    service = _service(path)
    _register(service)
    _verify(service)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DROP TRIGGER audit_events_no_update")
        conn.execute("UPDATE audit_events SET payload_json=? WHERE seq=1", (payload,))
    assert _assert_eligibility(service, "deny").checks["audit_chain_intact"] is False
