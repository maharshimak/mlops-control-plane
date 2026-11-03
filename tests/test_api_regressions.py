"""Regression tests for API defects confirmed on main."""

import pytest
from fastapi.testclient import TestClient

from mlops_cp import api
from mlops_cp.registry import ModelRegistry


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("MLOPS_CP_API_TOKEN", raising=False)
    monkeypatch.setattr(api, "registry", ModelRegistry())
    return TestClient(api.app)


def _register(client: TestClient, name: str, version: str):
    return client.post(
        "/v1/models",
        json={
            "name": name,
            "version": version,
            "artifact_uri": f"s3://synthetic/{name}/{version}",
            "dataset_fingerprint": "synthetic-dataset",
        },
    )


def test_repeated_candidate_promotion_is_a_conflict_not_a_server_error(client) -> None:
    assert _register(client, "fraud", "1").status_code == 200
    client.post(
        "/v1/models/fraud/1/evaluations",
        json={"metric": "auc", "value": 0.9, "threshold": 0.8},
    )
    assert client.post("/v1/models/fraud/1/candidate").json()["allowed"] is True

    repeated = client.post("/v1/models/fraud/1/candidate")

    assert repeated.status_code == 409
    assert "candidate -> candidate" in repeated.json()["detail"]


def test_colon_in_name_or_version_is_rejected_to_prevent_key_collisions(client) -> None:
    # "a:b" + "c" and "a" + "b:c" both produced the registry key "a:b:c".
    assert _register(client, "a:b", "c").status_code == 422
    assert _register(client, "a", "b:c").status_code == 422
    assert _register(client, "a", "b").status_code == 200


def test_blank_name_is_rejected(client) -> None:
    assert _register(client, "   ", "1").status_code == 422
