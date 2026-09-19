from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class DeploymentTelemetry:
    requests: int
    errors: int
    p95_latency_ms: float
    baseline_error_rate: float
    baseline_p95_latency_ms: float

    def __post_init__(self) -> None:
        if isinstance(self.requests, bool) or not isinstance(self.requests, int):
            raise TypeError("requests must be an integer")
        if isinstance(self.errors, bool) or not isinstance(self.errors, int):
            raise TypeError("errors must be an integer")
        if self.requests <= 0 or not 0 <= self.errors <= self.requests:
            raise ValueError("require requests > 0 and 0 <= errors <= requests")
        for name, value in (
            ("p95_latency_ms", self.p95_latency_ms),
            ("baseline_error_rate", self.baseline_error_rate),
            ("baseline_p95_latency_ms", self.baseline_p95_latency_ms),
        ):
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.baseline_error_rate > 1:
            raise ValueError("baseline_error_rate must be at most 1")
        if self.baseline_p95_latency_ms == 0:
            raise ValueError("baseline_p95_latency_ms must be greater than zero")


@dataclass(frozen=True, slots=True)
class RollbackPolicy:
    min_requests: int = 100
    max_error_rate: float = 0.05
    max_error_rate_increase: float = 0.02
    max_latency_ratio: float = 1.5

    def __post_init__(self) -> None:
        if isinstance(self.min_requests, bool) or not isinstance(self.min_requests, int):
            raise TypeError("min_requests must be an integer")
        if self.min_requests <= 0:
            raise ValueError("min_requests must be positive")
        for name, value in (
            ("max_error_rate", self.max_error_rate),
            ("max_error_rate_increase", self.max_error_rate_increase),
            ("max_latency_ratio", self.max_latency_ratio),
        ):
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.max_error_rate > 1 or self.max_error_rate_increase > 1:
            raise ValueError("error-rate thresholds must be at most 1")
        if self.max_latency_ratio < 1:
            raise ValueError("max_latency_ratio must be at least 1")


@dataclass(frozen=True, slots=True)
class RollbackDecision:
    rollback: bool
    reasons: tuple[str, ...]
    error_rate: float
    latency_ratio: float


def decide_rollback(
    telemetry: DeploymentTelemetry,
    policy: RollbackPolicy | None = None,
) -> RollbackDecision:
    policy = policy or RollbackPolicy()
    error_rate = telemetry.errors / telemetry.requests
    latency_ratio = telemetry.p95_latency_ms / telemetry.baseline_p95_latency_ms
    reasons: list[str] = []

    if telemetry.requests < policy.min_requests:
        reasons.append(
            f"insufficient evidence: {telemetry.requests} requests < {policy.min_requests}"
        )
        return RollbackDecision(
            rollback=False,
            reasons=tuple(reasons),
            error_rate=error_rate,
            latency_ratio=latency_ratio,
        )

    if error_rate > policy.max_error_rate:
        reasons.append(
            f"error rate {error_rate:.3f} > maximum {policy.max_error_rate:.3f}"
        )
    if error_rate - telemetry.baseline_error_rate > policy.max_error_rate_increase:
        reasons.append(
            "error-rate increase "
            f"{error_rate - telemetry.baseline_error_rate:.3f} > maximum "
            f"{policy.max_error_rate_increase:.3f}"
        )
    if latency_ratio > policy.max_latency_ratio:
        reasons.append(
            f"p95 latency ratio {latency_ratio:.3f} > maximum {policy.max_latency_ratio:.3f}"
        )

    return RollbackDecision(
        rollback=bool(reasons),
        reasons=tuple(reasons),
        error_rate=error_rate,
        latency_ratio=latency_ratio,
    )
