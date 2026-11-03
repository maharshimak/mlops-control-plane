"""Validated schema for candidate evaluations handed over by a training pipeline.

The schema is the hand-off point for evidence produced by a training pipeline
such as ``mlops-production-pipeline``: dataset fingerprint, configuration
digest, code version, metrics with thresholds and the SHA-256 of the produced
artifact. All digests are claims made by the producer; the control plane only
re-derives what it can (``config_sha256`` when ``config`` is supplied, and the
artifact digest through ``verify_artifact``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mlops_cp.audit import sha256_json
from mlops_cp.models import MODEL_NAME_PATTERN, MODEL_VERSION_PATTERN, Evaluation

EVIDENCE_SCHEMA_VERSION = "candidate-evidence.v1"
HEX64 = r"^[0-9a-f]{64}$"


class MetricEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
    value: float = Field(allow_inf_nan=False)
    threshold: float = Field(allow_inf_nan=False)
    higher_is_better: bool = True

    def to_evaluation(self) -> Evaluation:
        return Evaluation(
            metric=self.name,
            value=self.value,
            threshold=self.threshold,
            higher_is_better=self.higher_is_better,
        )


class CandidateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["candidate-evidence.v1"] = EVIDENCE_SCHEMA_VERSION
    producer: str = Field(min_length=1, max_length=200)
    model_name: str = Field(pattern=MODEL_NAME_PATTERN)
    model_version: str = Field(pattern=MODEL_VERSION_PATTERN)
    artifact_uri: str = Field(min_length=1, max_length=2000)
    artifact_sha256: str = Field(pattern=HEX64)
    dataset_fingerprint: str = Field(pattern=HEX64)
    config_sha256: str = Field(pattern=HEX64)
    config: dict[str, Any] | None = None
    code_version: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    metrics: list[MetricEvidence] = Field(min_length=1, max_length=200)
    submitted_by: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("metrics")
    @classmethod
    def _unique_metrics(cls, metrics: list[MetricEvidence]) -> list[MetricEvidence]:
        names = [item.name for item in metrics]
        if len(names) != len(set(names)):
            raise ValueError("metric names must be unique")
        return metrics

    @model_validator(mode="after")
    def _config_digest_matches(self) -> CandidateEvidence:
        if self.config is not None:
            try:
                actual = sha256_json(self.config)
            except ValueError as error:  # NaN/inf are not canonical JSON
                raise ValueError("config must be JSON-serialisable without NaN/inf") from error
            if actual != self.config_sha256:
                raise ValueError("config_sha256 does not match the canonical JSON of config")
        return self

    @property
    def model_key(self) -> str:
        return f"{self.model_name}:{self.model_version}"

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def sha256(self) -> str:
        return sha256_json(self.canonical())
