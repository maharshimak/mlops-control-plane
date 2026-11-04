"""Governed promotion: candidate evidence, artifact integrity, approvals and drift.

``GovernanceService`` composes the existing :class:`ModelRegistry` with an
append-only hash-chained :class:`AuditLog` and two insert-only tables
(candidate evidence and approvals). Its promotion policy reports *every*
failing reason instead of stopping at the first one:

* the audit chain verifies,
* the model is in a promotable lifecycle stage,
* candidate evidence was recorded and matches the registry row,
* the artifact bytes were verified against the registered SHA-256,
* all evaluation gates pass and every required metric is present,
* no drift report reaches the configured denial severity,
* enough distinct approvals were recorded (approvals alone missing yields
  ``requires_approval`` instead of ``deny``).

The policy is configured on the service, never by the caller of a decision, so
a request cannot relax its own gates. Nothing here deploys a model: production
promotion changes the registry stage only.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mlops_cp.artifacts import (
    ArtifactVerification,
    compare_digest,
    digest_bytes,
    digest_file,
    resolve_confined,
)
from mlops_cp.audit import AuditLog, sha256_json, verify_chain
from mlops_cp.drift import drift_severity, population_stability_index
from mlops_cp.evidence import CandidateEvidence
from mlops_cp.models import ModelVersion, utc_now
from mlops_cp.policy import evaluate_promotion as evaluate_basic_gates
from mlops_cp.registry import ModelRegistry
from mlops_cp.storage import connect, transaction


def _evaluation_digest(model: ModelVersion) -> str:
    return sha256_json(json.dumps([asdict(item) for item in model.evaluations], sort_keys=True))


Decision = Literal["allow", "deny", "requires_approval"]
_SEVERITY_RANK = {"stable": 0, "warning": 1, "critical": 2}


class GovernanceError(ValueError):
    """A governance precondition failed (conflict, missing evidence, ...)."""

    def __init__(self, message: str, *, code: str = "PRECONDITION_FAILED") -> None:
        super().__init__(message)
        self.code = code


class DriftObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feature: str = Field(min_length=1, max_length=200)
    expected: list[float] = Field(min_length=1, max_length=10_000)
    actual: list[float] = Field(min_length=1, max_length=10_000)


@dataclass(frozen=True, slots=True)
class PromotionPolicyConfig:
    required_approvals: int = 1
    required_metrics: tuple[str, ...] = ()
    deny_drift_severity: Literal["warning", "critical"] = "critical"
    require_drift_evidence: bool = False
    require_artifact_verification: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.required_approvals, bool) or not isinstance(
            self.required_approvals, int
        ):
            raise TypeError("required_approvals must be an integer")
        if self.required_approvals < 0:
            raise ValueError("required_approvals must be non-negative")
        if self.deny_drift_severity not in {"warning", "critical"}:
            raise ValueError("deny_drift_severity must be 'warning' or 'critical'")


@dataclass(frozen=True, slots=True)
class Approval:
    model_key: str
    approver: str
    evidence_sha256: str
    note: str
    created_at: str


@dataclass(frozen=True, slots=True)
class GovernedDecision:
    model_key: str
    decision: Decision
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    checks: dict[str, bool]
    evidence_sha256: str | None
    approvals: tuple[str, ...]
    drift: tuple[dict[str, Any], ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_key": self.model_key,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "checks": dict(self.checks),
            "evidence_sha256": self.evidence_sha256,
            "approvals": list(self.approvals),
            "drift": [dict(item) for item in self.drift],
        }


class GovernanceService:
    def __init__(
        self,
        registry: ModelRegistry | None = None,
        *,
        database_path: str | None = None,
        policy: PromotionPolicyConfig | None = None,
        artifact_root: str | None = None,
        trusted_inputs: bool = True,
    ) -> None:
        if database_path == ":memory:":
            raise ValueError("use database_path=None for in-memory governance state")
        path = str(Path(database_path).expanduser().resolve()) if database_path else None
        self.registry = registry if registry is not None else ModelRegistry(path)
        if path is not None and self.registry.database_path != path:
            raise ValueError("Registry and governance must share one SQLite database.")
        self.database_path = self.registry.database_path
        self.registry._governed = True
        self.policy = policy or PromotionPolicyConfig()
        self.artifact_root = artifact_root
        self.trusted_inputs = trusted_inputs
        self.audit = AuditLog(self.database_path)
        if self.database_path is None:
            self.audit._lock = self.registry._lock
        self._evidence: dict[str, tuple[CandidateEvidence, str]] = {}
        self._approvals: list[Approval] = []
        if self.database_path is not None:
            self._initialize_storage()

    # ------------------------------------------------------------------ storage
    @contextmanager
    def _write(self) -> Iterator[None]:
        if self.database_path is not None:
            with transaction(self.database_path):
                yield
            return
        # In-memory operations share the registry lock. Its writes replace model
        # objects, so restoring the original mappings also restores the old stages.
        with self.registry._lock:
            models = self.registry._models.copy()
            lifecycle_length = len(self.registry._events)
            evidence = self._evidence.copy()
            approvals_length = len(self._approvals)
            audit_length = len(self.audit._events)
            try:
                yield
            except BaseException:
                self.registry._models = models
                del self.registry._events[lifecycle_length:]
                self._evidence = evidence
                del self._approvals[approvals_length:]
                del self.audit._events[audit_length:]
                raise

    def _initialize_storage(self) -> None:
        assert self.database_path is not None
        with connect(self.database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidate_evidence (
                    model_key TEXT PRIMARY KEY,
                    evidence_json TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS candidate_evidence_no_update
                BEFORE UPDATE ON candidate_evidence
                BEGIN SELECT RAISE(ABORT, 'candidate_evidence is insert-only'); END;
                CREATE TRIGGER IF NOT EXISTS candidate_evidence_no_delete
                BEFORE DELETE ON candidate_evidence
                BEGIN SELECT RAISE(ABORT, 'candidate_evidence is insert-only'); END;
                CREATE TABLE IF NOT EXISTS approvals (
                    model_key TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (model_key, approver)
                );
                CREATE TRIGGER IF NOT EXISTS approvals_no_update
                BEFORE UPDATE ON approvals
                BEGIN SELECT RAISE(ABORT, 'approvals is insert-only'); END;
                CREATE TRIGGER IF NOT EXISTS approvals_no_delete
                BEFORE DELETE ON approvals
                BEGIN SELECT RAISE(ABORT, 'approvals is insert-only'); END;
                """
            )

    def evidence(self, model_key: str) -> tuple[CandidateEvidence, str] | None:
        if self.database_path is None:
            with self.registry._lock:
                recorded = self._evidence.get(model_key)
                return (recorded[0].model_copy(deep=True), recorded[1]) if recorded else None
        with connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT evidence_json, evidence_sha256 FROM candidate_evidence "
                "WHERE model_key = ?",
                (model_key,),
            ).fetchone()
        if row is None:
            return None
        evidence = CandidateEvidence.model_validate(json.loads(row["evidence_json"]))
        return evidence, row["evidence_sha256"]

    def _store_evidence(self, evidence: CandidateEvidence, digest: str) -> None:
        if self.database_path is None:
            if evidence.model_key in self._evidence:
                raise GovernanceError(
                    f"evidence already recorded for {evidence.model_key}", code="CONFLICT"
                )
            self._evidence[evidence.model_key] = (evidence.model_copy(deep=True), digest)
            return
        try:
            with transaction(self.database_path) as connection:
                connection.execute(
                    "INSERT INTO candidate_evidence(model_key, evidence_json, "
                    "evidence_sha256, recorded_at) VALUES (?, ?, ?, ?)",
                    (evidence.model_key, json.dumps(evidence.canonical(), sort_keys=True),
                     digest, utc_now()),
                )
        except sqlite3.IntegrityError as error:
            raise GovernanceError(
                f"evidence already recorded for {evidence.model_key}", code="CONFLICT"
            ) from error

    def approvals(self, model_key: str) -> list[Approval]:
        if self.database_path is None:
            with self.registry._lock:
                return [item for item in self._approvals if item.model_key == model_key]
        with connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT * FROM approvals WHERE model_key = ? ORDER BY created_at, approver",
                (model_key,),
            ).fetchall()
        return [Approval(**dict(row)) for row in rows]

    # --------------------------------------------------------------- operations
    def register_candidate(self, evidence: CandidateEvidence) -> tuple[ModelVersion, str]:
        """Atomically register validated evidence, model state and audit records."""
        evidence = CandidateEvidence.model_validate(evidence.canonical())
        digest = evidence.sha256()
        model = ModelVersion(
            name=evidence.model_name,
            version=evidence.model_version,
            artifact_uri=evidence.artifact_uri,
            dataset_fingerprint=evidence.dataset_fingerprint,
            evaluations=[metric.to_evaluation() for metric in evidence.metrics],
        )
        with self._write():
            try:
                self.registry.register(model)
            except ValueError as error:
                raise GovernanceError(str(error), code="CONFLICT") from error
            self._store_evidence(evidence, digest)
            self.audit.append(
                "candidate_registered",
                evidence.model_key,
                {
                    "evidence_sha256": digest,
                    "artifact_sha256": evidence.artifact_sha256,
                    "dataset_fingerprint": evidence.dataset_fingerprint,
                    "config_sha256": evidence.config_sha256,
                    "code_version": evidence.code_version,
                    "producer": evidence.producer,
                    "trusted_source": self.trusted_inputs,
                    "metrics": sorted(item.name for item in evidence.metrics),
                },
            )
        return model, digest

    def verify_artifact(
        self,
        name: str,
        version: str,
        *,
        path: str | None = None,
        content: bytes | None = None,
    ) -> ArtifactVerification:
        if (path is None) == (content is None):
            raise GovernanceError("provide exactly one of path or content")
        with self._write():
            model_key = f"{name}:{version}"
            self.registry.get(name, version)  # KeyError when unknown
            recorded = self.evidence(model_key)
            if recorded is None:
                raise GovernanceError(f"no candidate evidence recorded for {model_key}")
            expected = recorded[0].artifact_sha256
            if path is not None:
                if not self.artifact_root:
                    raise PermissionError("artifact root is not configured")
                digest = digest_file(resolve_confined(self.artifact_root, path))
            else:
                assert content is not None
                digest = digest_bytes(content)
            result = compare_digest(expected, digest)
            self.audit.append(
                "artifact_verification",
                model_key,
                {
                    "verified": result.verified,
                    "expected_sha256": result.expected_sha256,
                    "actual_sha256": result.actual_sha256,
                    "size_bytes": result.size_bytes,
                    "source": "path" if path is not None else "bytes",
                    "relative_path": path,
                    "trusted_source": self.trusted_inputs,
                },
            )
            return result

    def record_approval(self, name: str, version: str, approver: str, note: str = "") -> Approval:
        approver = approver.strip()
        if not approver or len(approver) > 200:
            raise GovernanceError("approver must be 1-200 characters")
        if len(note) > 2000:
            raise GovernanceError("note must be at most 2000 characters")
        if not self.trusted_inputs:
            raise GovernanceError("Approval requires a trusted individual principal.")
        model_key = f"{name}:{version}"
        with self._write():
            model = self.registry.get(name, version)
            recorded = self.evidence(model_key)
            if recorded is None:
                raise GovernanceError(f"no candidate evidence recorded for {model_key}")
            evidence, digest = recorded
            if evidence.submitted_by is None or evidence.submitted_by.strip() == approver:
                raise GovernanceError("separation of duties requires distinct submitter and approver")
            if digest != evidence.sha256():
                raise GovernanceError("candidate evidence digest does not match")
            snapshot = _evaluation_digest(model)
            prior = next((item for item in self.approvals(model_key) if item.approver == approver),
                         None)
            prior_review = next(
                (event for event in reversed(self.audit.events(model_key))
                 if event.event_type == "approval_recorded"
                 and event.payload.get("approver") == approver),
                None,
            )
            if prior_review and prior_review.payload.get("evaluations_sha256") == snapshot:
                raise GovernanceError(f"{approver} already approved {model_key}", code="CONFLICT")
            approval = Approval(model_key, approver, digest, note, utc_now())
            if prior is None:
                if self.database_path is None:
                    self._approvals.append(approval)
                else:
                    with transaction(self.database_path) as connection:
                        connection.execute(
                            "INSERT INTO approvals(model_key, approver, evidence_sha256, note, "
                            "created_at) VALUES (?, ?, ?, ?, ?)",
                            (approval.model_key, approval.approver, approval.evidence_sha256,
                             approval.note, approval.created_at),
                        )
            self.audit.append(
                "approval_recorded",
                model_key,
                {"approver": approver, "evidence_sha256": digest,
                 "evaluations_sha256": snapshot, "approval_note": note},
            )
        return approval


    def evaluate_promotion(
        self,
        name: str,
        version: str,
        *,
        drift: Sequence[DriftObservation] = (),
    ) -> GovernedDecision:
        """Evaluate production eligibility against a consistent read snapshot."""
        if self.database_path is None:
            with self.registry._lock:
                return self._evaluate_promotion(name, version, drift)
        with transaction(self.database_path, write=False):
            return self._evaluate_promotion(name, version, drift)

    def _evaluate_promotion(
        self, name: str, version: str, drift: Sequence[DriftObservation]
    ) -> GovernedDecision:
        model = self.registry.get(name, version)  # KeyError when unknown
        model_key = model.key
        policy = self.policy
        reasons: list[str] = []
        warnings: list[str] = []
        checks: dict[str, bool] = {}

        model_events = []
        try:
            audit_events = self.audit.events()
            chain = verify_chain(audit_events)
        except (TypeError, ValueError):
            checks["audit_chain_intact"] = False
            reasons.append("audit log contains a malformed record")
        else:
            checks["audit_chain_intact"] = chain.valid
            if chain.valid:
                model_events = [event for event in audit_events if event.subject == model_key]
            else:
                reasons.append(
                    f"audit log integrity failure at seq {chain.first_invalid_seq}: {chain.reason}"
                )

        checks["lifecycle_stage"] = model.stage in {"registered", "candidate"}
        if not checks["lifecycle_stage"]:
            reasons.append(f"model stage {model.stage!r} cannot be promoted to production")

        try:
            recorded = self.evidence(model_key)
        except (TypeError, ValueError):
            recorded = None
            reasons.append("candidate evidence is malformed")
        else:
            if recorded is None:
                reasons.append("no candidate evidence recorded")
        evidence, evidence_digest = recorded if recorded else (None, None)
        checks["evidence_recorded"] = evidence is not None
        registration = next(
            (event.payload for event in reversed(model_events)
             if event.event_type == "candidate_registered"),
            {},
        )
        checks["trusted_promotion_service"] = self.trusted_inputs
        if not self.trusted_inputs:
            reasons.append("production requires a trusted governance service")
        checks["trusted_registration"] = (
            evidence_digest is not None
            and registration.get("evidence_sha256") == evidence_digest
            and registration.get("trusted_source") is True
        )
        if not checks["trusted_registration"]:
            reasons.append("production requires trusted candidate registration")
        if evidence is not None:
            consistent = (
                evidence.artifact_uri == model.artifact_uri
                and evidence.dataset_fingerprint == model.dataset_fingerprint
            )
            checks["evidence_digest_verified"] = (
                evidence.sha256() == evidence_digest
                and registration.get("evidence_sha256") == evidence_digest
            )
            if not checks["evidence_digest_verified"]:
                reasons.append("candidate evidence digest does not match the audited registration")
            checks["evidence_matches_registry"] = consistent
            if not consistent:
                reasons.append("registry artifact URI or dataset fingerprint differs from evidence")
            if evidence.pipeline_gate is not None:
                # The producing pipeline's own quality gate is binding, never advisory.
                checks["pipeline_gate"] = evidence.pipeline_gate.allowed
                if not evidence.pipeline_gate.allowed:
                    reasons.append(
                        "pipeline gate rejected candidate: "
                        + "; ".join(evidence.pipeline_gate.reasons)
                    )
            if evidence.code_version == "unknown":
                warnings.append("code_version is 'unknown'; the training code is not pinned")

        latest = next(
            (event.payload for event in reversed(model_events)
             if event.event_type == "artifact_verification"),
            {},
        )
        checks["trusted_artifact_verification"] = (
            not policy.require_artifact_verification or latest.get("trusted_source") is True
        )
        if policy.require_artifact_verification:
            if not checks["trusted_artifact_verification"]:
                reasons.append("production requires trusted artifact verification")
            verified = False
            if evidence is not None:
                if latest:
                    verified = (
                        latest.get("verified") is True
                        and latest.get("actual_sha256") == evidence.artifact_sha256
                        and latest.get("expected_sha256") == evidence.artifact_sha256
                    )
                    if verified and latest.get("source") == "path":
                        verified = self._verify_current_file(
                            model, latest.get("relative_path"), evidence.artifact_sha256
                        )
                    elif verified:
                        verified = latest.get("source") == "bytes"
                    if not verified:
                        reasons.append("latest artifact verification failed")
                else:
                    reasons.append("artifact has not been verified against its SHA-256")
            checks["artifact_verified"] = verified
        checks["trusted_promotion_inputs"] = (
            checks["trusted_promotion_service"]
            and checks["trusted_registration"]
            and checks["trusted_artifact_verification"]
        )

        gates = evaluate_basic_gates(model)
        present = {item.name for item in evidence.metrics} if evidence is not None else set()
        missing = sorted(set(policy.required_metrics) - present)
        checks["evaluation_gates"] = gates.allowed and not missing
        reasons.extend(f"evaluation gate: {reason}" for reason in gates.reasons)
        if missing:
            reasons.append("missing required metrics: " + ", ".join(missing))

        drift_results: list[dict[str, Any]] = []
        deny_rank = _SEVERITY_RANK[policy.deny_drift_severity]
        for observation in drift:
            psi = population_stability_index(observation.expected, observation.actual)
            severity = drift_severity(psi)
            drift_results.append({"feature": observation.feature, "psi": psi,
                                  "severity": severity})
            if _SEVERITY_RANK[severity] >= deny_rank:
                reasons.append(f"{severity} drift on feature {observation.feature!r} "
                               f"(PSI {psi:.4f})")
            elif severity != "stable":
                warnings.append(f"{severity} drift on feature {observation.feature!r} "
                                f"(PSI {psi:.4f})")
        drift_ok = all(_SEVERITY_RANK[d["severity"]] < deny_rank for d in drift_results)
        if not drift_results:
            if policy.require_drift_evidence:
                drift_ok = False
                reasons.append("drift evidence is required but none was supplied")
            else:
                warnings.append("no drift evidence supplied")
        checks["drift_within_policy"] = drift_ok

        snapshot = _evaluation_digest(model)
        reviews = {
            event.payload.get("approver"): event.payload
            for event in model_events if event.event_type == "approval_recorded"
        }
        approvers = tuple(
            item.approver
            for item in self.approvals(model_key)
            if self.trusted_inputs and evidence is not None and evidence.submitted_by
            and item.approver != evidence.submitted_by.strip()
            and item.evidence_sha256 == evidence_digest
            and reviews.get(item.approver, {}).get("evidence_sha256") == evidence_digest
            and reviews.get(item.approver, {}).get("evaluations_sha256") == snapshot
        )
        approvals_ok = len(approvers) >= policy.required_approvals
        checks["approvals"] = approvals_ok

        if reasons:
            decision: Decision = "deny"
            if not approvals_ok:
                reasons.append(f"requires {policy.required_approvals} approval(s), "
                               f"has {len(approvers)}")
        elif not approvals_ok:
            decision = "requires_approval"
            reasons.append(f"requires {policy.required_approvals} approval(s), "
                           f"has {len(approvers)}")
        else:
            decision = "allow"

        return GovernedDecision(
            model_key=model_key,
            decision=decision,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            checks=checks,
            evidence_sha256=evidence_digest,
            approvals=approvers,
            drift=tuple(drift_results),
        )

    def _verify_current_file(
        self, model: ModelVersion, relative_path: str | None, expected_sha256: str
    ) -> bool:
        if self.artifact_root is None or not isinstance(relative_path, str):
            return False
        try:
            path = resolve_confined(self.artifact_root, relative_path)
            return (
                model.artifact_uri == path.as_uri()
                and compare_digest(expected_sha256, digest_file(path)).verified
            )
        except (OSError, ValueError):
            return False

    def promote_to_production(
        self,
        name: str,
        version: str,
        *,
        drift: Sequence[DriftObservation] = (),
    ) -> GovernedDecision:
        """Move the registry stage to production only when the policy allows it.

        This changes registry state and the audit log; it does not deploy.
        """
        with self._write():
            decision = self.evaluate_promotion(name, version, drift=drift)
            if decision.decision != "allow":
                return decision
            model = self.registry.get(name, version)
            if (
                model.stage == "registered"
                and not self.registry.promote_candidate(name, version).allowed
            ):
                raise GovernanceError("registry candidate gate rejected the model")
            self.registry._promote_production(name, version)
            self.audit.append(
                "promoted_to_production",
                decision.model_key,
                {"evidence_sha256": decision.evidence_sha256,
                 "approvals": list(decision.approvals)},
            )
            return decision
