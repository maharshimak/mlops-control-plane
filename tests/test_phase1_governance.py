import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Barrier, Thread
from urllib.error import HTTPError

import pytest
from conftest import ARTIFACT_BYTES, evidence_payload
from fastapi.testclient import TestClient

from mlops_cp import api
from mlops_cp.deployments import DeploymentCommand, HTTPDeploymentTarget
from mlops_cp.drift import population_stability_index
from mlops_cp.evidence import CandidateEvidence
from mlops_cp.governed import GovernanceError, GovernanceService, PromotionPolicyConfig
from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.registry import ModelRegistry


def _registered(service, **overrides):
    service.register_candidate(CandidateEvidence.model_validate(evidence_payload(**overrides)))


def _abort(path, table, predicate="1"):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            f"CREATE TRIGGER inject_failure BEFORE INSERT ON {table} WHEN {predicate} "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )


def test_legacy_http_promotion_cannot_bypass_governance(monkeypatch):
    registry = ModelRegistry()
    monkeypatch.setattr(api, "registry", registry)
    monkeypatch.setattr(api, "governance", GovernanceService(registry))
    model = ModelVersion("legacy", "1", "synthetic", "dataset",
                         evaluations=[Evaluation("auc", .9, .8)])
    registry.register(model)
    assert registry.promote_candidate("legacy", "1").allowed
    response = TestClient(api.app).post("/v1/models/legacy/1/production")
    assert response.status_code == 403
    assert registry.get("legacy", "1").stage == "candidate"


def test_shared_bearer_cannot_assert_a_different_approver(monkeypatch):
    service = GovernanceService()
    _registered(service)
    monkeypatch.setattr(api, "governance", service)
    monkeypatch.setenv("MLOPS_CP_API_TOKEN", "synthetic-shared-key")
    response = TestClient(api.app).post(
        "/v1/governance/fraud/1.0.0/approvals",
        headers={"Authorization": "Bearer synthetic-shared-key", "X-Principal": "reviewer"},
        json={"approver": "reviewer"},
    )
    assert response.status_code == 403
    assert service.approvals("fraud:1.0.0") == []


def test_failed_legacy_registration_leaves_no_registry_row(tmp_path):
    path = str(tmp_path / "registry.db")
    registry = ModelRegistry(path)
    _abort(path, "lifecycle_events")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        registry.register(ModelVersion("legacy", "1", "synthetic", "dataset"))
    assert registry.list() == []


def test_failed_governed_registration_leaves_no_state(tmp_path):
    path = str(tmp_path / "governed.db")
    service = GovernanceService(database_path=path)
    _abort(path, "audit_events")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        _registered(service)
    assert service.registry.list() == []
    assert service.registry.history("fraud", "1.0.0") == []
    assert service.evidence("fraud:1.0.0") is None


def test_failed_governed_promotion_rolls_back_every_transition(tmp_path):
    path = str(tmp_path / "promotion.db")
    service = GovernanceService(database_path=path, policy=PromotionPolicyConfig(required_approvals=0))
    _registered(service, model_version="0.9.0")
    service.verify_artifact("fraud", "0.9.0", content=ARTIFACT_BYTES)
    assert service.promote_to_production("fraud", "0.9.0").decision == "allow"
    _registered(service)
    service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    _abort(path, "audit_events", "NEW.event_type = 'promoted_to_production'")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        service.promote_to_production("fraud", "1.0.0")
    assert service.registry.get("fraud", "0.9.0").stage == "production"
    assert [event.to_stage for event in service.registry.history("fraud", "0.9.0")] == [
        "registered", "candidate", "production"
    ]
    assert service.registry.get("fraud", "1.0.0").stage == "registered"
    assert [event.to_stage for event in service.registry.history("fraud", "1.0.0")] == [
        "registered"
    ]


def test_approval_becomes_stale_when_evaluations_change(tmp_path):
    service = GovernanceService(database_path=str(tmp_path / "stale.db"))
    _registered(service)
    service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    service.record_approval("fraud", "1.0.0", "reviewer")
    service.registry.add_evaluation("fraud", "1.0.0", Evaluation("precision", .9, .8))
    decision = service.promote_to_production("fraud", "1.0.0")
    assert decision.decision == "requires_approval"
    assert service.registry.get("fraud", "1.0.0").stage == "registered"


def test_promotion_rechecks_file_after_successful_verification(tmp_path):
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(ARTIFACT_BYTES)
    service = GovernanceService(artifact_root=str(tmp_path),
                                policy=PromotionPolicyConfig(required_approvals=0))
    _registered(service, artifact_uri=artifact.as_uri())
    assert service.verify_artifact("fraud", "1.0.0", path="model.bin").verified
    assert service.evaluate_promotion("fraud", "1.0.0").decision == "allow"
    artifact.write_bytes(b"substituted")
    assert service.promote_to_production("fraud", "1.0.0").decision == "deny"


def test_promotion_rejects_verification_of_a_different_path(tmp_path):
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(ARTIFACT_BYTES)
    service = GovernanceService(artifact_root=str(tmp_path),
                                policy=PromotionPolicyConfig(required_approvals=0))
    _registered(service, artifact_uri=(tmp_path / "unrelated.bin").as_uri())
    assert service.verify_artifact("fraud", "1.0.0", path="model.bin").verified
    assert service.promote_to_production("fraud", "1.0.0").decision == "deny"


def test_simultaneous_governed_promotions_record_one_winner(tmp_path):
    path = str(tmp_path / "concurrent.db")
    policy = PromotionPolicyConfig(required_approvals=0)
    services = [GovernanceService(database_path=path, policy=policy) for _ in range(8)]
    _registered(services[0])
    services[0].verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    barrier = Barrier(len(services))

    def promote(service):
        barrier.wait(timeout=10)
        return service.promote_to_production("fraud", "1.0.0").decision

    with ThreadPoolExecutor(max_workers=len(services)) as pool:
        assert sorted(pool.map(promote, services)) == ["allow"] + ["deny"] * 7
    assert [event.event_type for event in services[0].audit.events()].count(
        "promoted_to_production"
    ) == 1


def test_extreme_finite_distributions_do_not_bypass_drift():
    assert population_stability_index([1e308, 1e308], [1.7e308, 1e307]) == pytest.approx(
        population_stability_index([1, 1], [17, 1])
    )


def test_failed_approval_audit_is_atomic(tmp_path):
    path = str(tmp_path / "approval.db")
    service = GovernanceService(database_path=path)
    _registered(service)
    _abort(path, "audit_events", "NEW.event_type = 'approval_recorded'")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        service.record_approval("fraud", "1.0.0", "reviewer")
    assert service.approvals("fraud:1.0.0") == []


def test_stale_approval_can_be_renewed():
    service = GovernanceService()
    _registered(service)
    service.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    service.record_approval("fraud", "1.0.0", "reviewer")
    service.registry.add_evaluation("fraud", "1.0.0", Evaluation("precision", .9, .8))
    assert service.evaluate_promotion("fraud", "1.0.0").decision == "requires_approval"
    service.record_approval("fraud", "1.0.0", "reviewer")
    assert service.promote_to_production("fraud", "1.0.0").decision == "allow"


def test_governed_registry_cannot_be_promoted_through_standalone_api(tmp_path):
    path = str(tmp_path / "managed.db")
    service = GovernanceService(database_path=path)
    _registered(service)
    standalone = ModelRegistry(path)
    assert standalone.promote_candidate("fraud", "1.0.0").allowed
    with pytest.raises(ValueError, match="GovernanceService"):
        standalone.promote_production("fraud", "1.0.0")
    assert standalone.get("fraud", "1.0.0").stage == "candidate"


def test_shared_credential_evidence_cannot_be_reused_for_trusted_commit(tmp_path):
    path = str(tmp_path / "shared.db")
    policy = PromotionPolicyConfig(required_approvals=0)
    shared = GovernanceService(database_path=path, policy=policy, trusted_inputs=False)
    _registered(shared)
    shared.verify_artifact("fraud", "1.0.0", content=ARTIFACT_BYTES)
    with pytest.raises(GovernanceError, match="individual principal"):
        shared.record_approval("fraud", "1.0.0", "spoofed")
    trusted = GovernanceService(database_path=path, policy=policy)
    assert trusted.evaluate_promotion("fraud", "1.0.0").decision == "allow"
    assert trusted.promote_to_production("fraud", "1.0.0").decision == "deny"


@pytest.fixture()
def controller():
    servers = []

    def start(handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 200])
def test_deployment_controller_receives_one_post_and_never_forwards_token(
    controller, status, monkeypatch
):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    first_requests = []
    second_requests = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            second_requests.append((self.command, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"accepted":true}')

        do_POST = do_GET

    destination = controller(Destination)

    class Configured(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            first_requests.append((self.command, self.headers.get("Authorization")))
            self.send_response(status)
            if status != 200:
                self.send_header("Location", destination)
            self.end_headers()
            if status == 200:
                self.wfile.write(b'{"accepted":true,"deployment_id":"synthetic-receipt"}')

    target = HTTPDeploymentTarget(controller(Configured), bearer_token="synthetic")
    command = DeploymentCommand("fraud:1.0.0", "synthetic", "deploy", 100, "test")
    if status == 200:
        receipt = target.execute(command)
        assert receipt.accepted and receipt.deployment_id == "synthetic-receipt"
    else:
        with pytest.raises(HTTPError) as caught:
            target.execute(command)
        assert caught.value.code == status
        caught.value.close()
    assert first_requests == [("POST", "Bearer synthetic")]
    assert second_requests == []
