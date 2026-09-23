from fastapi.testclient import TestClient

from mlops_cp.api import app


def test_remote_token_mode_requires_bearer(monkeypatch):
    monkeypatch.setenv("MLOPS_CP_API_TOKEN", "test-secret")
    client = TestClient(app)

    denied = client.get("/v1/models")
    assert denied.status_code == 401

    allowed = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer test-secret"},
    )
    assert allowed.status_code == 200
