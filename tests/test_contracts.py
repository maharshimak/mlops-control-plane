import base64
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import ARTIFACT_BYTES, ARTIFACT_SHA256, evidence_payload
from jsonschema import Draft202012Validator, FormatChecker

from mlops_cp.contracts import capabilities, run_operation
from mlops_cp.governed import GovernanceService, PromotionPolicyConfig

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contracts" / "makma.v1.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

ARTIFACT_B64 = base64.b64encode(ARTIFACT_BYTES).decode()
TELEMETRY_BAD = {"requests": 1000, "errors": 100, "p95_latency_ms": 300,
                 "baseline_error_rate": 0.01, "baseline_p95_latency_ms": 100}
TELEMETRY_OK = {"requests": 1000, "errors": 10, "p95_latency_ms": 110,
                "baseline_error_rate": 0.01, "baseline_p95_latency_ms": 100}
SNAP = {"requests": 500, "error_rate": 0.01, "p95_latency_ms": 100, "quality_score": 0.9}


def valid(envelope: dict) -> dict:
    errors = sorted(VALIDATOR.iter_errors(envelope), key=str)
    assert errors == [], [e.message for e in errors]
    assert envelope["execution_mode"] == "deterministic_offline"
    assert envelope["system"] == "mlops-control-plane"
    # FormatChecker skips date-time without optional deps, so check RFC 3339 UTC here.
    assert envelope["started_at"].endswith("Z")
    assert datetime.fromisoformat(envelope["started_at"]).utcoffset() == timedelta(0)
    return envelope


@pytest.fixture()
def service() -> GovernanceService:
    return GovernanceService(policy=PromotionPolicyConfig(required_approvals=1))


def run(service, operation, payload, **kwargs):
    return valid(run_operation(operation, payload, service=service, **kwargs))


def test_vendored_schema_is_the_shared_contract() -> None:
    assert SCHEMA["$id"].endswith("/contracts/makma.v1.schema.json")
    Draft202012Validator.check_schema(SCHEMA)


def test_capabilities_declare_real_operations_with_valid_input_schemas() -> None:
    caps = capabilities()
    assert caps["contract_version"] == "makma.v1"
    assert caps["system"] == "mlops-control-plane"
    names = [op["name"] for op in caps["operations"]]
    assert names == ["register_candidate", "verify_artifact", "evaluate_promotion",
                     "rollback_decision", "audit_log"]
    for op in caps["operations"]:
        Draft202012Validator.check_schema(op["input_schema"])
        assert op["timeout_s"] > 0
    json.dumps(caps)  # serialisable


def test_governed_lifecycle_through_the_contract(service) -> None:
    registered = run(service, "register_candidate", evidence_payload(),
                     run_id="run-1", correlation_id="corr-1")
    assert registered["status"] == "ok"
    assert registered["run_id"] == "run-1" and registered["correlation_id"] == "corr-1"
    assert registered["data"]["model_key"] == "fraud:1.0.0"
    assert registered["data"]["artifact_verified"] is False
    kinds = {item["kind"]: item for item in registered["provenance"]}
    assert kinds["record"]["sha256"] == registered["data"]["evidence_sha256"]
    assert kinds["artifact"]["sha256"] is None  # only a claim until verified

    unverified = run(service, "evaluate_promotion",
                     {"model_name": "fraud", "model_version": "1.0.0"})
    assert unverified["status"] == "blocked"
    assert unverified["error"]["code"] == "PROMOTION_DENIED"
    assert unverified["data"] is None
    assert unverified["policy"]["decision"] == "deny"
    assert "artifact has not been verified against its SHA-256" in unverified["policy"]["reasons"]

    verified = run(service, "verify_artifact", {"model_name": "fraud", "model_version": "1.0.0",
                                                "content_base64": ARTIFACT_B64})
    assert verified["status"] == "ok" and verified["data"]["verified"] is True
    assert verified["provenance"][0]["sha256"] == ARTIFACT_SHA256

    pending = run(service, "evaluate_promotion", {
        "model_name": "fraud", "model_version": "1.0.0",
        "drift": [{"feature": "amount", "expected": [50, 50], "actual": [49, 51]}],
    })
    assert pending["status"] == "pending_approval"
    assert pending["policy"] == {"decision": "requires_approval",
                                 "reasons": ["requires 1 approval(s), has 0"]}
    assert pending["data"]["drift"][0]["severity"] == "stable"

    service.record_approval("fraud", "1.0.0", "risk-officer")
    allowed = run(service, "evaluate_promotion", {"model_name": "fraud", "model_version": "1.0.0"})
    assert allowed["status"] == "ok"
    assert allowed["policy"]["decision"] == "allow"
    # evaluate_promotion is advisory: it never changes the registry stage.
    assert service.registry.get("fraud", "1.0.0").stage == "registered"

    audit = run(service, "audit_log", {"model_name": "fraud", "model_version": "1.0.0"})
    assert audit["data"]["verification"]["valid"] is True
    assert [e["event_type"] for e in audit["data"]["events"]] == [
        "candidate_registered", "artifact_verification", "approval_recorded"
    ]
    head = audit["data"]["verification"]["head_hash"]
    anchored = run(service, "audit_log", {"expected_head": "0" * 64})
    assert anchored["data"]["verification"]["valid"] is False
    assert run(service, "audit_log", {"expected_head": head})["data"]["verification"]["valid"]


def test_artifact_mismatch_is_reported_not_hidden(service) -> None:
    run(service, "register_candidate", evidence_payload())
    result = run(service, "verify_artifact", {
        "model_name": "fraud", "model_version": "1.0.0",
        "content_base64": base64.b64encode(b"tampered").decode(),
    })
    assert result["status"] == "ok"
    assert result["data"]["verified"] is False
    assert result["data"]["expected_sha256"] == ARTIFACT_SHA256


@pytest.mark.parametrize(
    ("operation", "payload", "status", "code"),
    [
        ("deploy_to_production", {}, "error", "UNKNOWN_OPERATION"),
        ("Bad Name!", {}, "error", "UNKNOWN_OPERATION"),
        ("register_candidate", {"model_name": "x"}, "error", "INVALID_INPUT"),
        ("register_candidate", evidence_payload(model_name="a:b"), "error", "INVALID_INPUT"),
        ("evaluate_promotion", {"model_name": "missing", "model_version": "1"}, "error",
         "NOT_FOUND"),
        ("verify_artifact", {"model_name": "fraud", "model_version": "1.0.0"}, "error",
         "INVALID_INPUT"),
        ("verify_artifact", {"model_name": "fraud", "model_version": "1.0.0",
                             "content_base64": "%%%"}, "error", "INVALID_INPUT"),
        ("rollback_decision", {}, "error", "INVALID_INPUT"),
        ("rollback_decision", {"telemetry": {**TELEMETRY_OK, "errors": 5000}}, "error",
         "INVALID_INPUT"),
        ("audit_log", {"model_name": "fraud"}, "error", "INVALID_INPUT"),
        ("audit_log", [], "error", "INVALID_INPUT"),
    ],
)
def test_error_envelopes_validate(service, operation, payload, status, code) -> None:
    envelope = run(service, operation, payload)
    assert envelope["status"] == status
    assert envelope["error"]["code"] == code
    assert envelope["data"] is None


def test_path_verification_without_artifact_root_is_blocked(service) -> None:
    run(service, "register_candidate", evidence_payload())
    envelope = run(service, "verify_artifact", {"model_name": "fraud", "model_version": "1.0.0",
                                                "path": "fraud/model.bin"})
    assert envelope["status"] == "blocked"
    assert envelope["error"]["code"] == "ARTIFACT_ROOT_NOT_CONFIGURED"


def test_path_traversal_is_an_access_error(tmp_path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    service = GovernanceService(artifact_root=str(root))
    run(service, "register_candidate", evidence_payload())
    envelope = run(service, "verify_artifact", {"model_name": "fraud", "model_version": "1.0.0",
                                                "path": "../../outside.bin"})
    assert envelope["error"]["code"] == "ARTIFACT_ACCESS_DENIED"


def test_duplicate_registration_is_a_conflict(service) -> None:
    run(service, "register_candidate", evidence_payload())
    assert run(service, "register_candidate", evidence_payload())["error"]["code"] == "CONFLICT"


def test_validation_details_do_not_echo_inputs(service) -> None:
    envelope = run(service, "register_candidate",
                   evidence_payload(artifact_sha256="SECRET-LOOKING-VALUE"))
    assert "SECRET-LOOKING-VALUE" not in json.dumps(envelope)


def test_overlong_run_id_is_rejected_with_a_valid_envelope(service) -> None:
    envelope = run(service, "audit_log", {}, run_id="x" * 129)
    assert envelope["error"]["code"] == "INVALID_INPUT"
    assert len(envelope["run_id"]) == 32


@pytest.mark.parametrize(
    ("payload", "action", "rollback"),
    [
        ({"telemetry": TELEMETRY_BAD}, "rollback", True),
        ({"telemetry": TELEMETRY_OK}, "continue", False),
        ({"telemetry": {**TELEMETRY_BAD, "requests": 20, "errors": 10}}, "hold", False),
        ({"canary": {"production": SNAP, "candidate": SNAP, "current_traffic_percent": 5}},
         "increase", False),
        ({"canary": {"production": SNAP, "candidate": SNAP, "current_traffic_percent": 50}},
         "promote", False),
        ({"canary": {"production": SNAP,
                     "candidate": {**SNAP, "quality_score": 0.5},
                     "current_traffic_percent": 25}}, "rollback", True),
        ({"canary": {"production": SNAP, "candidate": {**SNAP, "requests": 10},
                     "current_traffic_percent": 25}}, "hold", False),
        # Telemetry rollback wins over a healthy canary.
        ({"telemetry": TELEMETRY_BAD,
          "canary": {"production": SNAP, "candidate": SNAP, "current_traffic_percent": 5}},
         "rollback", True),
        # Insufficient telemetry evidence holds even if the canary would advance.
        ({"telemetry": {**TELEMETRY_OK, "requests": 20, "errors": 0},
          "canary": {"production": SNAP, "candidate": SNAP, "current_traffic_percent": 5}},
         "hold", False),
    ],
)
def test_rollback_decisions_are_deterministic(service, payload, action, rollback) -> None:
    first = run(service, "rollback_decision", payload)
    second = run(service, "rollback_decision", payload)
    assert first["status"] == "ok"
    assert first["data"] == second["data"]
    assert first["provenance"] == second["provenance"]
    assert first["data"]["action"] == action
    assert first["data"]["rollback"] is rollback
    assert first["data"]["deployment_executed"] is False
    if rollback:
        assert first["data"]["reasons"]


def test_invalid_canary_stage_is_invalid_input(service) -> None:
    envelope = run(service, "rollback_decision", {
        "canary": {"production": SNAP, "candidate": SNAP, "current_traffic_percent": 7},
    })
    assert envelope["error"]["code"] == "INVALID_INPUT"


def test_unexpected_failures_do_not_leak_internals(service, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("database password=hunter2")

    monkeypatch.setattr(service, "evaluate_promotion", boom)
    envelope = run(service, "evaluate_promotion", {"model_name": "a", "model_version": "1"})
    assert envelope["error"]["code"] == "INTERNAL_ERROR"
    assert "hunter2" not in json.dumps(envelope)
