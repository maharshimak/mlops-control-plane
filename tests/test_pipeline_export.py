"""Integration with real mlops-production-pipeline output (see tests/fixtures/README.md)."""

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_contracts import valid

from mlops_cp.contracts import run_operation
from mlops_cp.evidence import (
    PipelineExportError,
    evidence_from_pipeline_export,
)
from mlops_cp.governed import GovernanceService, PromotionPolicyConfig

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PASS = json.loads((FIXTURES / "pipeline_candidate_pass.json").read_text(encoding="utf-8"))
FAILED = json.loads((FIXTURES / "pipeline_candidate_gate_failed.json").read_text(encoding="utf-8"))
ARTIFACT = (FIXTURES / "pipeline_candidate_pass.model.json").read_bytes()
FAILED_ARTIFACT = (FIXTURES / "pipeline_candidate_gate_failed.model.json").read_bytes()
IDENTITY = {"model_name": "fraud-ridge", "model_version": "2026.11.03",
            "artifact_uri": "s3://synthetic-models/fraud-ridge/model.json",
            "submitted_by": "pipeline-bot"}
REF = {"model_name": "fraud-ridge", "model_version": "2026.11.03"}


@pytest.fixture()
def service() -> GovernanceService:
    return GovernanceService(policy=PromotionPolicyConfig(required_approvals=1))


def register(service, export):
    return valid(run_operation("register_candidate", {"pipeline_export": export, **IDENTITY},
                               service=service))


def test_fixtures_are_consistent_pipeline_output() -> None:
    assert PASS["format"] == FAILED["format"] == "mlops-candidate-evaluation.v1"
    assert hashlib.sha256(ARTIFACT).hexdigest() == PASS["artifact_sha256"]
    assert hashlib.sha256(FAILED_ARTIFACT).hexdigest() == FAILED["artifact_sha256"]
    assert PASS["gate"] == {"allowed": True, "reasons": []}
    assert FAILED["gate"]["allowed"] is False and FAILED["gate"]["reasons"]


def test_vendored_schema_matches_the_schema_used_by_the_mapping() -> None:
    vendored = Path(__file__).resolve().parents[1] / "src" / "mlops_cp" / "schemas"
    schema = json.loads((vendored / "candidate-evaluation.v1.schema.json").read_text("utf-8"))
    assert schema["properties"]["format"]["const"] == "mlops-candidate-evaluation.v1"


def test_mapping_is_explicit_and_invents_no_thresholds() -> None:
    evidence = evidence_from_pipeline_export(PASS, **IDENTITY)
    assert evidence.producer == "mlops-production-pipeline"
    assert evidence.source_format == "mlops-candidate-evaluation.v1"
    assert evidence.dataset_fingerprint == PASS["dataset_sha256"]
    assert evidence.config_sha256 == PASS["config_sha256"]
    assert evidence.artifact_sha256 == PASS["artifact_sha256"]
    assert evidence.code_version == PASS["code_version"]
    assert evidence.run_record_sha256 == PASS["run_record_sha256"]
    assert evidence.pipeline_model_id == PASS["model_id"]
    assert evidence.observed_metrics == PASS["metrics"]
    assert [(m.name, m.value, m.threshold) for m in evidence.metrics] == [
        ("pipeline_gate", 1.0, 1.0)
    ]


def test_real_pipeline_export_goes_end_to_end_to_allow(service) -> None:
    registered = register(service, PASS)
    assert registered["status"] == "ok"
    assert registered["data"]["source_format"] == "mlops-candidate-evaluation.v1"
    assert registered["data"]["pipeline_gate"] == {"allowed": True, "reasons": []}

    verified = valid(run_operation("verify_artifact", {
        **REF, "content_base64": base64.b64encode(ARTIFACT).decode(),
    }, service=service))
    assert verified["data"]["verified"] is True

    pending = valid(run_operation("evaluate_promotion", REF, service=service))
    assert pending["status"] == "pending_approval"
    assert pending["data"]["checks"]["pipeline_gate"] is True

    service.record_approval("fraud-ridge", "2026.11.03", "risk-officer")
    allowed = valid(run_operation("evaluate_promotion", REF, service=service))
    assert allowed["status"] == "ok" and allowed["policy"]["decision"] == "allow"


def test_failed_pipeline_gate_blocks_promotion_even_when_everything_else_passes() -> None:
    service = GovernanceService(policy=PromotionPolicyConfig(required_approvals=0))
    registered = register(service, FAILED)
    # Registration records the evidence (including the rejection) for the audit trail ...
    assert registered["status"] == "ok"
    assert registered["data"]["pipeline_gate"]["allowed"] is False
    assert service.verify_artifact("fraud-ridge", "2026.11.03", content=FAILED_ARTIFACT).verified

    decision = valid(run_operation("evaluate_promotion", REF, service=service))
    # ... but promotion is refused with the pipeline's own reasons.
    assert decision["status"] == "blocked"
    assert decision["error"]["code"] == "PROMOTION_DENIED"
    reasons = decision["policy"]["reasons"]
    assert "pipeline gate rejected candidate: " + FAILED["gate"]["reasons"][0] in reasons
    assert decision["error"]["details"]["checks"]["pipeline_gate"] is False
    assert service.promote_to_production("fraud-ridge", "2026.11.03").decision == "deny"
    assert service.registry.get("fraud-ridge", "2026.11.03").stage == "registered"


def _tampered(mutator):
    export = copy.deepcopy(PASS)
    mutator(export)
    return export


@pytest.mark.parametrize(
    ("name", "export"),
    [
        ("uppercase hash", _tampered(lambda e: e.update(
            artifact_sha256=e["artifact_sha256"].upper()))),
        ("short dataset hash", _tampered(lambda e: e.update(dataset_sha256="abc123"))),
        ("wrong format", _tampered(lambda e: e.update(format="mlops-candidate-evaluation.v2"))),
        ("extra field", _tampered(lambda e: e.update(approved=True))),
        ("missing metric", _tampered(lambda e: e["metrics"].pop("test_mae"))),
        ("negative metric", _tampered(lambda e: e["metrics"].update(test_mae=-1.0))),
        ("gate allowed with reasons",
         _tampered(lambda e: e["gate"].update(reasons=["edited after the fact"]))),
        ("gate rejected without reasons", _tampered(lambda e: e["gate"].update(allowed=False))),
        ("swapped artifact digest",
         _tampered(lambda e: e.update(artifact_sha256=hashlib.sha256(b"other").hexdigest()))),
        ("not an object", ["not", "an", "object"]),
    ],
)
def test_tampered_exports_are_refused(service, name, export) -> None:
    envelope = register(service, export)
    assert envelope["status"] == "error", name
    assert envelope["error"]["code"] == "INVALID_INPUT"
    assert envelope["data"] is None
    with pytest.raises(KeyError):
        service.registry.get("fraud-ridge", "2026.11.03")
    assert service.audit.events() == []


def test_schema_errors_report_locations_without_values() -> None:
    secret_like = "Z" * 64
    with pytest.raises(PipelineExportError) as error:
        evidence_from_pipeline_export(
            _tampered(lambda e: e.update(run_record_sha256=secret_like)), **IDENTITY
        )
    assert error.value.errors == [{"path": ["run_record_sha256"], "validator": "pattern"}]
    assert secret_like not in json.dumps(error.value.errors)


def test_non_finite_metric_passed_in_process_is_refused(service) -> None:
    # jsonschema's "minimum" does not reject NaN; the evidence model must.
    envelope = register(service, _tampered(lambda e: e["metrics"].update(test_mae=float("nan"))))
    assert envelope["error"]["code"] == "INVALID_INPUT"


def test_unknown_code_version_is_accepted_with_a_warning() -> None:
    service = GovernanceService(policy=PromotionPolicyConfig(required_approvals=0))
    register(service, _tampered(lambda e: e.update(code_version="unknown")))
    service.verify_artifact("fraud-ridge", "2026.11.03", content=ARTIFACT)
    decision = service.evaluate_promotion("fraud-ridge", "2026.11.03")
    assert decision.decision == "allow"
    assert any("code_version is 'unknown'" in item for item in decision.warnings)
