"""``makma.v1`` execution envelope and operations for MAK'MA AI OS.

Every operation here is rule-based and runs offline, so ``execution_mode`` is
always ``deterministic_offline``. Envelopes are validated in tests against the
vendored ``contracts/makma.v1.schema.json``.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import metadata
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    RootModel,
    Tag,
    ValidationError,
    model_validator,
)

from mlops_cp.artifacts import ArtifactAccessError
from mlops_cp.audit import sha256_json
from mlops_cp.canary import CanarySnapshot
from mlops_cp.evidence import (
    CandidateEvidence,
    PipelineExportError,
    evidence_from_pipeline_export,
)
from mlops_cp.governed import (
    DriftObservation,
    GovernanceError,
    GovernanceService,
    PromotionPolicyConfig,
)
from mlops_cp.models import MODEL_NAME_PATTERN, MODEL_VERSION_PATTERN
from mlops_cp.progressive import RolloutPolicy, evaluate_progressive_rollout
from mlops_cp.rollback import DeploymentTelemetry, RollbackPolicy, decide_rollback

CONTRACT_VERSION = "makma.v1"
SYSTEM = "mlops-control-plane"
EXECUTION_MODE = "deterministic_offline"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_OPERATION_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- envelope
class ContractError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    message: str = Field(max_length=2000)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ProvenanceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["document", "chunk", "artifact", "dataset", "model", "query", "record"]
    id: str = Field(min_length=1)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    span: tuple[int, int] | None = None
    uri: str | None = None


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["allow", "deny", "requires_approval"]
    reasons: list[str]


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["makma.v1"] = CONTRACT_VERSION
    system: Literal["mlops-control-plane"] = SYSTEM
    operation: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    run_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    status: Literal["ok", "error", "blocked", "pending_approval"]
    execution_mode: Literal["deterministic_offline", "local_model", "remote_model"]
    data: Any = None
    error: ContractError | None = None
    provenance: list[ProvenanceRef] = Field(default_factory=list)
    policy: PolicyDecision | None = None
    started_at: str
    duration_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def _status_rules(self) -> Envelope:
        if (self.status == "ok") != (self.error is None) and self.status != "pending_approval":
            raise ValueError("status 'ok' requires error=null and vice versa")
        if self.status in {"error", "blocked"} and (self.error is None or self.data is not None):
            raise ValueError("error/blocked envelopes need an error and data=null")
        if self.status == "pending_approval" and (
            self.policy is None or self.policy.decision != "requires_approval"
        ):
            raise ValueError("pending_approval requires policy.decision=requires_approval")
        return self


class OperationOutcome(BaseModel):
    """What an operation handler returns before it is wrapped in an envelope."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: Literal["ok", "error", "blocked", "pending_approval"] = "ok"
    data: Any = None
    error: ContractError | None = None
    provenance: list[ProvenanceRef] = Field(default_factory=list)
    policy: PolicyDecision | None = None


class OperationFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: Literal["error", "blocked"] = "error",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.details = details or {}


# ------------------------------------------------------------------ op inputs
class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelRef(_Input):
    model_name: str = Field(pattern=MODEL_NAME_PATTERN)
    model_version: str = Field(pattern=MODEL_VERSION_PATTERN)


class VerifyArtifactInput(ModelRef):
    path: str | None = Field(default=None, min_length=1, max_length=1000)
    content_base64: str | None = Field(
        default=None, min_length=1, max_length=(MAX_ARTIFACT_BYTES * 4) // 3 + 4
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> VerifyArtifactInput:
        if (self.path is None) == (self.content_base64 is None):
            raise ValueError("provide exactly one of path or content_base64")
        return self


class EvaluatePromotionInput(ModelRef):
    drift: list[DriftObservation] = Field(default_factory=list, max_length=500)


class SnapshotInput(_Input):
    requests: int = Field(ge=0)
    error_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    p95_latency_ms: float = Field(gt=0, allow_inf_nan=False)
    quality_score: float = Field(ge=0, le=1, allow_inf_nan=False)


class CanaryInput(_Input):
    production: SnapshotInput
    candidate: SnapshotInput
    current_traffic_percent: int = Field(ge=0, le=100)
    stages: list[int] | None = Field(default=None, min_length=1, max_length=20)
    min_requests_per_stage: int = Field(default=100, ge=1)
    max_error_rate_increase: float = Field(default=0.01, ge=0, allow_inf_nan=False)
    max_latency_increase_ms: float = Field(default=250.0, ge=0, allow_inf_nan=False)
    max_quality_drop: float = Field(default=0.02, ge=0, allow_inf_nan=False)


class TelemetryInput(_Input):
    requests: int = Field(gt=0)
    errors: int = Field(ge=0)
    p95_latency_ms: float = Field(ge=0, allow_inf_nan=False)
    baseline_error_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    baseline_p95_latency_ms: float = Field(gt=0, allow_inf_nan=False)


class RollbackPolicyInput(_Input):
    min_requests: int = Field(default=100, ge=1)
    max_error_rate: float = Field(default=0.05, ge=0, le=1, allow_inf_nan=False)
    max_error_rate_increase: float = Field(default=0.02, ge=0, le=1, allow_inf_nan=False)
    max_latency_ratio: float = Field(default=1.5, ge=1, allow_inf_nan=False)


class RollbackDecisionInput(_Input):
    telemetry: TelemetryInput | None = None
    policy: RollbackPolicyInput = Field(default_factory=RollbackPolicyInput)
    canary: CanaryInput | None = None

    @model_validator(mode="after")
    def _some_evidence(self) -> RollbackDecisionInput:
        if self.telemetry is None and self.canary is None:
            raise ValueError("provide telemetry, canary, or both")
        if self.telemetry is not None and self.telemetry.errors > self.telemetry.requests:
            raise ValueError("telemetry.errors cannot exceed telemetry.requests")
        return self


class AuditLogInput(_Input):
    model_name: str | None = Field(default=None, pattern=MODEL_NAME_PATTERN)
    model_version: str | None = Field(default=None, pattern=MODEL_VERSION_PATTERN)
    limit: int = Field(default=100, ge=1, le=1000)
    expected_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _both_or_neither(self) -> AuditLogInput:
        if (self.model_name is None) != (self.model_version is None):
            raise ValueError("model_name and model_version must be given together")
        return self


class PipelineRegistrationInput(_Input):
    """Wraps an ``mlops-candidate-evaluation.v1`` export with registry identity.

    The export is validated against the vendored JSON Schema in the handler.
    """

    pipeline_export: dict[str, Any]
    model_name: str = Field(pattern=MODEL_NAME_PATTERN)
    model_version: str = Field(pattern=MODEL_VERSION_PATTERN)
    artifact_uri: str = Field(min_length=1, max_length=2000)
    submitted_by: str | None = Field(default=None, min_length=1, max_length=200)


def _register_input_kind(value: Any) -> str:
    return "pipeline" if isinstance(value, dict) and "pipeline_export" in value else "evidence"


class RegisterCandidateInput(RootModel[
    Annotated[
        Annotated[PipelineRegistrationInput, Tag("pipeline")]
        | Annotated[CandidateEvidence, Tag("evidence")],
        Discriminator(_register_input_kind),
    ]
]):
    pass


# ---------------------------------------------------------------- handlers
def _register_candidate(
    service: GovernanceService, parsed: RegisterCandidateInput
) -> OperationOutcome:
    request = parsed.root
    if isinstance(request, PipelineRegistrationInput):
        try:
            payload = evidence_from_pipeline_export(
                request.pipeline_export,
                model_name=request.model_name,
                model_version=request.model_version,
                artifact_uri=request.artifact_uri,
                submitted_by=request.submitted_by,
            )
        except PipelineExportError as error:
            raise OperationFailure(
                "INVALID_INPUT", str(error), details={"errors": error.errors}
            ) from error
        except ValidationError as error:
            raise OperationFailure(
                "INVALID_INPUT", "pipeline export could not be mapped to candidate evidence",
                details=_validation_details(error),
            ) from error
    else:
        payload = request
    model, digest = service.register_candidate(payload)
    return OperationOutcome(
        data={
            "model_key": model.key,
            "stage": model.stage,
            "evidence_sha256": digest,
            "claimed_artifact_sha256": payload.artifact_sha256,
            "artifact_verified": False,
            "evaluations": [asdict(item) | {"passed": item.passed} for item in model.evaluations],
            "source_format": payload.source_format or payload.schema_version,
            "pipeline_model_id": payload.pipeline_model_id,
            "run_record_sha256": payload.run_record_sha256,
            "pipeline_gate": (
                payload.pipeline_gate.model_dump() if payload.pipeline_gate else None
            ),
            "observed_metrics": dict(payload.observed_metrics),
            "audit_head": service.audit.verify().head_hash,
        },
        provenance=[
            ProvenanceRef(kind="record", id=f"candidate-evidence:{model.key}", sha256=digest),
            # The artifact digest is the producer's claim until verify_artifact runs.
            ProvenanceRef(kind="artifact", id=model.key, sha256=None, uri=payload.artifact_uri),
            ProvenanceRef(kind="dataset", id=payload.dataset_fingerprint, sha256=None),
        ],
    )


def _verify_artifact(service: GovernanceService, payload: VerifyArtifactInput) -> OperationOutcome:
    content = None
    if payload.content_base64 is not None:
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise OperationFailure("INVALID_INPUT", "content_base64 is not valid base64") from error
        if len(content) > MAX_ARTIFACT_BYTES:
            raise OperationFailure("INVALID_INPUT", "artifact content exceeds the size limit")
    try:
        result = service.verify_artifact(
            payload.model_name, payload.model_version, path=payload.path, content=content
        )
    except PermissionError as error:
        raise OperationFailure(
            "ARTIFACT_ROOT_NOT_CONFIGURED",
            "path-based verification requires a configured artifact root",
            status="blocked",
        ) from error
    model_key = f"{payload.model_name}:{payload.model_version}"
    return OperationOutcome(
        data=asdict(result) | {"model_key": model_key},
        provenance=[ProvenanceRef(kind="artifact", id=model_key, sha256=result.actual_sha256)],
    )


def _evaluate_promotion(
    service: GovernanceService, payload: EvaluatePromotionInput
) -> OperationOutcome:
    """Preview the same production eligibility policy used by a governed commit."""
    decision = service.evaluate_promotion(
        payload.model_name, payload.model_version, drift=payload.drift
    )
    policy = PolicyDecision(decision=decision.decision, reasons=list(decision.reasons))
    provenance = [ProvenanceRef(kind="model", id=decision.model_key)]
    if decision.evidence_sha256:
        provenance.append(
            ProvenanceRef(
                kind="record",
                id=f"candidate-evidence:{decision.model_key}",
                sha256=decision.evidence_sha256,
            )
        )
    if decision.decision == "deny":
        return OperationOutcome(
            status="blocked",
            error=ContractError(
                code="PROMOTION_DENIED",
                message="promotion policy denied the model",
                details=decision.to_dict(),
            ),
            provenance=provenance,
            policy=policy,
        )
    return OperationOutcome(
        status="pending_approval" if decision.decision == "requires_approval" else "ok",
        data=decision.to_dict(),
        provenance=provenance,
        policy=policy,
    )


def _rollback_decision(
    service: GovernanceService, payload: RollbackDecisionInput
) -> OperationOutcome:
    del service
    reasons: list[str] = []
    data: dict[str, Any] = {"deployment_executed": False}
    rollback = False
    action = "continue"
    try:
        if payload.telemetry is not None:
            telemetry_decision = decide_rollback(
                DeploymentTelemetry(**payload.telemetry.model_dump()),
                RollbackPolicy(**payload.policy.model_dump()),
            )
            data["telemetry_decision"] = asdict(telemetry_decision) | {
                "reasons": list(telemetry_decision.reasons)
            }
            if telemetry_decision.rollback:
                rollback = True
                reasons.extend(f"telemetry: {r}" for r in telemetry_decision.reasons)
            elif telemetry_decision.reasons:  # insufficient evidence
                action = "hold"
                reasons.extend(f"telemetry: {r}" for r in telemetry_decision.reasons)
        if payload.canary is not None:
            canary = payload.canary
            rollout_kwargs: dict[str, Any] = {
                "min_requests_per_stage": canary.min_requests_per_stage,
                "max_error_rate_increase": canary.max_error_rate_increase,
                "max_latency_increase_ms": canary.max_latency_increase_ms,
                "max_quality_drop": canary.max_quality_drop,
            }
            if canary.stages is not None:
                rollout_kwargs["stages"] = tuple(canary.stages)
            rollout = evaluate_progressive_rollout(
                CanarySnapshot(**canary.production.model_dump()),
                CanarySnapshot(**canary.candidate.model_dump()),
                canary.current_traffic_percent,
                policy=RolloutPolicy(**rollout_kwargs),
            )
            data["canary_decision"] = asdict(rollout) | {"reasons": list(rollout.reasons)}
            if rollout.action == "rollback":
                rollback = True
                reasons.extend(f"canary: {r}" for r in rollout.reasons)
            elif action != "hold":
                action = rollout.action
                reasons.extend(f"canary: {r}" for r in rollout.reasons)
            else:
                reasons.extend(f"canary: {r}" for r in rollout.reasons)
    except (TypeError, ValueError) as error:
        raise OperationFailure("INVALID_INPUT", str(error)) from error
    data["rollback"] = rollback
    data["action"] = "rollback" if rollback else action
    data["reasons"] = reasons
    return OperationOutcome(
        data=data,
        provenance=[ProvenanceRef(kind="record", id="rollback-input",
                                  sha256=sha256_json(payload.model_dump(mode="json")))],
    )


def _audit_log(service: GovernanceService, payload: AuditLogInput) -> OperationOutcome:
    verification = service.audit.verify(expected_head=payload.expected_head)
    subject = (
        f"{payload.model_name}:{payload.model_version}" if payload.model_name else None
    )
    events = service.audit.events(subject)
    selected = events[-payload.limit:]
    return OperationOutcome(
        data={
            "verification": asdict(verification),
            "subject": subject,
            "total_events": len(events),
            "events": [event.to_dict() for event in selected],
        },
        provenance=[ProvenanceRef(kind="record", id=f"audit-event:{e.seq}", sha256=e.hash)
                    for e in selected],
    )


_OPERATIONS: dict[str, tuple[type[BaseModel], Callable[..., OperationOutcome], bool, int]] = {
    # name: (input model, handler, side_effects, timeout_s)
    "register_candidate": (RegisterCandidateInput, _register_candidate, True, 30),
    "verify_artifact": (VerifyArtifactInput, _verify_artifact, True, 120),
    "evaluate_promotion": (EvaluatePromotionInput, _evaluate_promotion, False, 30),
    "rollback_decision": (RollbackDecisionInput, _rollback_decision, False, 10),
    "audit_log": (AuditLogInput, _audit_log, False, 30),
}


# ---------------------------------------------------------------- public API
def _package_version() -> str:
    try:
        return metadata.version("mlops-control-plane")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def capabilities() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "system": SYSTEM,
        "version": _package_version(),
        "operations": [
            {
                "name": name,
                "side_effects": side_effects,
                "requires_approval": False,
                "input_schema": model.model_json_schema(),
                "timeout_s": timeout,
            }
            for name, (model, _, side_effects, timeout) in _OPERATIONS.items()
        ],
    }


_default_service: GovernanceService | None = None


def policy_from_env() -> PromotionPolicyConfig:
    """Promotion policy from server-side configuration (never from request payloads)."""
    approvals = int(os.environ.get("MLOPS_CP_REQUIRED_APPROVALS", "1"))
    required = tuple(
        item.strip()
        for item in os.environ.get("MLOPS_CP_REQUIRED_METRICS", "").split(",")
        if item.strip()
    )
    return PromotionPolicyConfig(required_approvals=approvals, required_metrics=required)


def service_from_env() -> GovernanceService:
    return GovernanceService(
        database_path=os.environ.get("MLOPS_CP_GOVERNANCE_DB_PATH") or None,
        policy=policy_from_env(),
        artifact_root=os.environ.get("MLOPS_CP_ARTIFACT_ROOT") or None,
    )


def get_default_service() -> GovernanceService:
    global _default_service
    if _default_service is None:
        _default_service = service_from_env()
    return _default_service


def _validation_details(error: ValidationError) -> dict[str, Any]:
    # Omit the offending input values: they may be large or sensitive.
    return {
        "errors": [
            {"loc": [str(part) for part in item["loc"]], "msg": item["msg"],
             "type": item["type"]}
            for item in error.errors()[:20]
        ]
    }


def run_operation(
    operation: str,
    payload: dict[str, Any] | None,
    *,
    run_id: str | None = None,
    correlation_id: str | None = None,
    service: GovernanceService | None = None,
) -> dict[str, Any]:
    started = datetime.now(UTC)
    clock = time.perf_counter()
    bad_ids = [
        name
        for name, value in (("run_id", run_id), ("correlation_id", correlation_id))
        if value is not None and not (isinstance(value, str) and 1 <= len(value) <= 128)
    ]
    run_id = run_id if run_id and "run_id" not in bad_ids else uuid.uuid4().hex
    if not correlation_id or "correlation_id" in bad_ids:
        correlation_id = run_id
    safe_operation = (
        operation
        if isinstance(operation, str) and _OPERATION_NAME.fullmatch(operation)
        else "unknown_operation"
    )

    try:
        if bad_ids:
            raise OperationFailure(
                "INVALID_INPUT", "run_id/correlation_id must be 1-128 characters",
                details={"fields": bad_ids},
            )
        spec = _OPERATIONS.get(operation)
        if spec is None:
            raise OperationFailure(
                "UNKNOWN_OPERATION",
                "unknown operation",
                details={"supported": sorted(_OPERATIONS)},
            )
        input_model, handler, _, _ = spec
        if not isinstance(payload, dict):
            raise OperationFailure("INVALID_INPUT", "payload must be a JSON object")
        try:
            parsed = input_model.model_validate(payload)
        except ValidationError as error:
            raise OperationFailure(
                "INVALID_INPUT", "payload failed validation", details=_validation_details(error)
            ) from error
        outcome = handler(service or get_default_service(), parsed)
    except OperationFailure as failure:
        outcome = OperationOutcome(
            status=failure.status,
            error=ContractError(code=failure.code, message=failure.message,
                                retryable=failure.retryable, details=failure.details),
        )
    except KeyError:
        outcome = OperationOutcome(
            status="error", error=ContractError(code="NOT_FOUND", message="model not found")
        )
    except ArtifactAccessError as error:
        outcome = OperationOutcome(
            status="error",
            error=ContractError(code="ARTIFACT_ACCESS_DENIED", message=str(error)),
        )
    except GovernanceError as error:
        outcome = OperationOutcome(
            status="error", error=ContractError(code=error.code, message=str(error))
        )
    except Exception:
        _logger.exception("makma.v1 operation %s failed", safe_operation)
        outcome = OperationOutcome(
            status="error",
            error=ContractError(code="INTERNAL_ERROR", message="operation failed",
                                retryable=True),
        )

    envelope = Envelope(
        operation=safe_operation,
        run_id=run_id,
        correlation_id=correlation_id,
        trace_id=secrets.token_hex(16),
        span_id=secrets.token_hex(8),
        status=outcome.status,
        execution_mode=EXECUTION_MODE,
        data=outcome.data if outcome.status in {"ok", "pending_approval"} else None,
        error=outcome.error,
        provenance=outcome.provenance,
        policy=outcome.policy,
        started_at=started.isoformat().replace("+00:00", "Z"),
        duration_ms=(time.perf_counter() - clock) * 1000,
    )
    return envelope.model_dump(mode="json")
