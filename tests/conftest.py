"""Shared synthetic fixtures. No real models, datasets or credentials."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import pytest

from mlops_cp.audit import sha256_json

ARTIFACT_BYTES = b"synthetic-model-weights-v1\x00\x01\x02"
ARTIFACT_SHA256 = hashlib.sha256(ARTIFACT_BYTES).hexdigest()
CONFIG = {"learning_rate": 0.1, "max_depth": 4, "seed": 7}


def evidence_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "candidate-evidence.v1",
        "producer": "mlops-production-pipeline",
        "model_name": "fraud",
        "model_version": "1.0.0",
        "artifact_uri": "s3://synthetic-models/fraud/1.0.0/model.bin",
        "artifact_sha256": ARTIFACT_SHA256,
        "dataset_fingerprint": hashlib.sha256(b"synthetic-dataset").hexdigest(),
        "config_sha256": sha256_json(CONFIG),
        "config": CONFIG,
        "code_version": "0123456789abcdef0123456789abcdef01234567",
        "metrics": [
            {"name": "roc_auc", "value": 0.91, "threshold": 0.85, "higher_is_better": True},
            {"name": "latency_ms", "value": 40.0, "threshold": 100.0,
             "higher_is_better": False},
        ],
        "submitted_by": "pipeline-bot",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def make_evidence() -> Callable[..., dict[str, Any]]:
    return evidence_payload
