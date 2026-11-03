import hashlib
import os

import pytest
from conftest import ARTIFACT_BYTES, ARTIFACT_SHA256, evidence_payload
from pydantic import ValidationError

from mlops_cp.artifacts import (
    ArtifactAccessError,
    compare_digest,
    digest_bytes,
    digest_file,
    resolve_confined,
)
from mlops_cp.evidence import CandidateEvidence


def test_valid_pipeline_evidence_round_trips_with_a_stable_digest() -> None:
    first = CandidateEvidence.model_validate(evidence_payload())
    second = CandidateEvidence.model_validate(
        evidence_payload(config={"seed": 7, "max_depth": 4, "learning_rate": 0.1})
    )
    assert first.model_key == "fraud:1.0.0"
    assert first.sha256() == second.sha256()
    assert len(first.metrics) == 2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"config": {"learning_rate": 0.2}}, "config_sha256 does not match"),
        ({"artifact_sha256": "ABC"}, "artifact_sha256"),
        ({"dataset_fingerprint": "not-a-digest"}, "dataset_fingerprint"),
        ({"code_version": "has space"}, "code_version"),
        ({"model_name": "fraud:prod"}, "model_name"),
        ({"model_version": ""}, "model_version"),
        ({"schema_version": "candidate-evidence.v0"}, "schema_version"),
        ({"unexpected": 1}, "unexpected"),
        ({"metrics": []}, "metrics"),
        ({"metrics": [{"name": "auc", "value": float("nan"), "threshold": 0.8}]}, "value"),
        (
            {"metrics": [{"name": "auc", "value": 0.9, "threshold": 0.8},
                         {"name": "auc", "value": 0.8, "threshold": 0.8}]},
            "unique",
        ),
    ],
)
def test_invalid_evidence_is_rejected(overrides, message) -> None:
    with pytest.raises(ValidationError, match=message):
        CandidateEvidence.model_validate(evidence_payload(**overrides))


def test_evidence_without_inline_config_keeps_the_claimed_digest() -> None:
    payload = evidence_payload()
    payload.pop("config")
    assert CandidateEvidence.model_validate(payload).config is None


def test_digests_match_hashlib(tmp_path) -> None:
    path = tmp_path / "model.bin"
    data = os.urandom(3 * 1024 * 1024 + 17)  # spans several read chunks
    path.write_bytes(data)
    assert digest_file(path).sha256 == hashlib.sha256(data).hexdigest()
    assert digest_file(path).size_bytes == len(data)
    assert digest_bytes(ARTIFACT_BYTES).sha256 == ARTIFACT_SHA256


def test_compare_digest_reports_mismatch_and_rejects_malformed_expectations() -> None:
    assert compare_digest(ARTIFACT_SHA256, digest_bytes(ARTIFACT_BYTES)).verified
    mismatch = compare_digest(ARTIFACT_SHA256, digest_bytes(b"other"))
    assert not mismatch.verified and mismatch.reason
    with pytest.raises(ValueError):
        compare_digest(ARTIFACT_SHA256.upper(), digest_bytes(ARTIFACT_BYTES))


def test_symlink_escaping_the_root_is_rejected(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.bin"
    outside.write_bytes(b"outside")
    link = root / "link.bin"
    try:
        link.symlink_to(outside)
    except OSError as error:  # Windows without symlink privilege
        pytest.skip(f"symlinks unavailable: {error}")
    with pytest.raises(ArtifactAccessError, match="escapes"):
        resolve_confined(root, "link.bin")


@pytest.mark.parametrize("bad", ["", "a\x00b", "../x", "/etc/passwd"])
def test_resolve_confined_rejects_unsafe_paths(tmp_path, bad) -> None:
    with pytest.raises(ArtifactAccessError):
        resolve_confined(tmp_path, bad)
