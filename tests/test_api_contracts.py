import base64

import pytest
from conftest import ARTIFACT_BYTES, evidence_payload
from fastapi.testclient import TestClient
from test_contracts import valid

from mlops_cp import api
from mlops_cp.governed import GovernanceService
from mlops_cp.registry import ModelRegistry

TOKEN = "synthetic-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("MLOPS_CP_API_TOKEN", TOKEN)
    registry = ModelRegistry()
    monkeypatch.setattr(api, "registry", registry)
    monkeypatch.setattr(api, "governance", GovernanceService(registry, trusted_inputs=False))
    return TestClient(api.app)


def test_contract_routes_require_the_existing_bearer_auth(client) -> None:
    assert client.get("/v1/capabilities").status_code == 401
    assert client.post("/v1/ops/audit_log", json={}).status_code == 401
    assert client.post("/v1/governance/fraud/1.0.0/approvals",
                       json={"approver": "x"}).status_code == 401
    caps = client.get("/v1/capabilities", headers=AUTH)
    assert caps.status_code == 200
    assert caps.json()["system"] == "mlops-control-plane"


def test_non_ascii_bearer_is_rejected_without_error(client) -> None:
    response = client.get("/v1/capabilities", headers=[(b"Authorization", b"Bearer \xe9")])
    assert response.status_code == 401


def test_ops_route_runs_the_governed_flow_and_shares_the_registry(client) -> None:
    registered = client.post("/v1/ops/register_candidate", json=evidence_payload(),
                             headers={**AUTH, "X-Run-Id": "api-run", "X-Correlation-Id": "c-1"})
    body = valid(registered.json())
    assert body["status"] == "ok" and body["run_id"] == "api-run"
    assert body["correlation_id"] == "c-1"
    # The contract registered the model in the same registry the REST routes use.
    models = client.get("/v1/models", headers=AUTH).json()
    assert [m["name"] for m in models] == ["fraud"]

    valid(client.post("/v1/ops/verify_artifact", headers=AUTH, json={
        "model_name": "fraud", "model_version": "1.0.0",
        "content_base64": base64.b64encode(ARTIFACT_BYTES).decode(),
    }).json())
    ref = {"model_name": "fraud", "model_version": "1.0.0"}
    pending = valid(client.post("/v1/ops/evaluate_promotion", headers=AUTH, json=ref).json())
    assert pending["status"] == "blocked"
    assert pending["policy"]["decision"] == "deny"
    assert pending["error"]["code"] == "PROMOTION_DENIED"

    denied = client.post("/v1/governance/fraud/1.0.0/approvals", headers=AUTH,
                         json={"approver": "pipeline-bot"})
    assert denied.status_code == 403
    spoofed = client.post("/v1/governance/fraud/1.0.0/approvals", headers=AUTH,
                          json={"approver": "risk-officer", "note": "synthetic review"})
    assert spoofed.status_code == 403
    assert api.governance.approvals("fraud:1.0.0") == []
    still_pending = valid(client.post("/v1/ops/evaluate_promotion", headers=AUTH, json=ref).json())
    assert still_pending["status"] == "blocked"
    assert still_pending["policy"]["decision"] == "deny"


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b"\"text\""])
def test_malformed_bodies_return_invalid_input_envelopes(client, raw) -> None:
    response = client.post("/v1/ops/audit_log", content=raw,
                           headers={**AUTH, "Content-Type": "application/json"})
    assert response.status_code == 200
    body = valid(response.json())
    assert body["error"]["code"] == "INVALID_INPUT"


def test_unknown_operation_over_http(client) -> None:
    body = valid(client.post("/v1/ops/deploy", json={}, headers=AUTH).json())
    assert body["error"]["code"] == "UNKNOWN_OPERATION"


def test_empty_body_is_treated_as_an_empty_object(client) -> None:
    body = valid(client.post("/v1/ops/audit_log", headers=AUTH).json())
    assert body["status"] == "ok"
    assert body["data"]["verification"]["valid"] is True
