"""Validated schema for candidate evaluations handed over by a training pipeline.

The schema is the hand-off point for evidence produced by a training pipeline
such as ``mlops-production-pipeline``: dataset fingerprint, configuration
digest, code version, metrics with thresholds and the SHA-256 of the produced
artifact. All digests are claims made by the producer; the control plane only
re-derives what it can (``config_sha256`` when ``config`` is supplied, and the
artifact digest through ``verify_artifact``).

``evidence_from_pipeline_export`` accepts the ``mlops-candidate-evaluation.v1``
document produced by ``mlops-production-pipeline``'s ``evaluate_candidate``
operation, validates it against the vendored JSON Schema
(``mlops_cp/schemas/candidate-evaluation.v1.schema.json``) and maps it field by
field into ``CandidateEvidence``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Annotated, Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mlops_cp.audit import sha256_json
from mlops_cp.models import MODEL_NAME_PATTERN, MODEL_VERSION_PATTERN, Evaluation

EVIDENCE_SCHEMA_VERSION = "candidate-evidence.v1"
PIPELINE_EXPORT_FORMAT = "mlops-candidate-evaluation.v1"
PIPELINE_PRODUCER = "mlops-production-pipeline"
PIPELINE_GATE_METRIC = "pipeline_gate"
HEX64 = r"^[0-9a-f]{64}$"


class PipelineExportError(ValueError):
    """The pipeline export failed schema validation or an integrity cross-check."""

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


class PipelineGate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reasons: list[str] = Field(max_length=200)

    @model_validator(mode="after")
    def _consistent(self) -> PipelineGate:
        if self.allowed == bool(self.reasons):
            raise ValueError("pipeline gate: allowed must be true exactly when reasons is empty")
        return self


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
    # Git SHA, release tag or the pipeline's literal "unknown" (reported as a warning).
    code_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$")
    metrics: list[MetricEvidence] = Field(min_length=1, max_length=200)
    submitted_by: str | None = Field(default=None, min_length=1, max_length=200)
    # Present only for evidence mapped from a pipeline export.
    source_format: Literal["mlops-candidate-evaluation.v1"] | None = None
    pipeline_model_id: str | None = Field(default=None, max_length=200)
    run_record_sha256: str | None = Field(default=None, pattern=HEX64)
    pipeline_gate: PipelineGate | None = None
    observed_metrics: dict[str, Annotated[float, Field(allow_inf_nan=False)]] = Field(
        default_factory=dict, max_length=200
    )

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


@lru_cache(maxsize=1)
def _pipeline_validator() -> Draft202012Validator:
    text = (
        resources.files("mlops_cp")
        .joinpath("schemas/candidate-evaluation.v1.schema.json")
        .read_text(encoding="utf-8")
    )
    schema = json.loads(text)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def evidence_from_pipeline_export(
    export: Any,
    *,
    model_name: str,
    model_version: str,
    artifact_uri: str,
    submitted_by: str | None = None,
) -> CandidateEvidence:
    """Validate a pipeline export and map it explicitly into ``CandidateEvidence``.

    Mapping: ``dataset_sha256`` -> ``dataset_fingerprint``; ``config_sha256``,
    ``artifact_sha256``, ``code_version`` and ``run_record_sha256`` unchanged;
    ``model_id`` -> ``pipeline_model_id``; ``metrics`` -> ``observed_metrics``
    (the export carries no thresholds, so none are invented); ``gate`` ->
    ``pipeline_gate`` plus one evaluation ``pipeline_gate`` (1.0 if allowed, else
    0.0, threshold 1.0) so the registry's own gate also fails closed.
    """
    errors = [
        # Report location and rule only; messages would echo the offending values.
        {"path": [str(part) for part in error.absolute_path], "validator": error.validator}
        for error in sorted(_pipeline_validator().iter_errors(export), key=lambda e: e.path)
    ][:20]
    if errors:
        raise PipelineExportError("pipeline export does not match candidate-evaluation.v1",
                                  errors)
    # model_id is "<pipeline model name>-<first 16 hex of artifact_sha256>".
    if export["model_id"].rsplit("-", 1)[1] != export["artifact_sha256"][:16]:
        raise PipelineExportError("model_id does not match artifact_sha256")
    gate = export["gate"]
    return CandidateEvidence(
        producer=PIPELINE_PRODUCER,
        model_name=model_name,
        model_version=model_version,
        artifact_uri=artifact_uri,
        artifact_sha256=export["artifact_sha256"],
        dataset_fingerprint=export["dataset_sha256"],
        config_sha256=export["config_sha256"],
        code_version=export["code_version"],
        metrics=[
            MetricEvidence(
                name=PIPELINE_GATE_METRIC,
                value=1.0 if gate["allowed"] else 0.0,
                threshold=1.0,
                higher_is_better=True,
            )
        ],
        submitted_by=submitted_by,
        source_format=PIPELINE_EXPORT_FORMAT,
        pipeline_model_id=export["model_id"],
        run_record_sha256=export["run_record_sha256"],
        pipeline_gate=PipelineGate(allowed=gate["allowed"], reasons=list(gate["reasons"])),
        observed_metrics={key: float(value) for key, value in export["metrics"].items()},
    )
