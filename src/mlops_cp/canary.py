from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class CanarySnapshot:
    requests: int
    error_rate: float
    p95_latency_ms: float
    quality_score: float

    def __post_init__(self) -> None:
        if type(self.requests) is not int or self.requests < 0:
            raise ValueError("requests must be a non-negative integer")
        for name, value in (("error_rate", self.error_rate), ("quality_score", self.quality_score)):
            if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be a finite probability")
        if (
            type(self.p95_latency_ms) not in (int, float)
            or not isfinite(self.p95_latency_ms)
            or self.p95_latency_ms <= 0
        ):
            raise ValueError("p95 latency must be finite and positive")


@dataclass(frozen=True, slots=True)
class CanaryDecision:
    action: str
    next_traffic_percent: int
    reasons: list[str]


def evaluate_canary(
    production: CanarySnapshot,
    candidate: CanarySnapshot,
    current_traffic_percent: int,
    *,
    min_requests: int = 100,
    max_error_rate_increase: float = 0.01,
    max_latency_increase_ms: float = 250.0,
    max_quality_drop: float = 0.02,
    step_percent: int = 20,
) -> CanaryDecision:
    for value, low, high in (
        (current_traffic_percent, 0, 100),
        (min_requests, 1, 10**9),
        (step_percent, 1, 100),
    ):
        if type(value) is not int or not low <= value <= high:
            raise ValueError("Invalid traffic or sample policy")
    for value in (max_error_rate_increase, max_latency_increase_ms, max_quality_drop):
        if type(value) not in (int, float) or not isfinite(value) or value < 0:
            raise ValueError("Regression budgets must be finite and non-negative")
    if candidate.requests < min_requests:
        return CanaryDecision(
            action="hold",
            next_traffic_percent=current_traffic_percent,
            reasons=["insufficient candidate traffic"],
        )

    reasons: list[str] = []
    if candidate.error_rate - production.error_rate > max_error_rate_increase:
        reasons.append("error-rate regression")
    if candidate.p95_latency_ms - production.p95_latency_ms > max_latency_increase_ms:
        reasons.append("latency regression")
    if production.quality_score - candidate.quality_score > max_quality_drop:
        reasons.append("quality regression")

    if reasons:
        return CanaryDecision(
            action="rollback",
            next_traffic_percent=0,
            reasons=reasons,
        )

    next_traffic = min(100, current_traffic_percent + step_percent)
    return CanaryDecision(
        action="promote" if next_traffic == 100 else "increase",
        next_traffic_percent=next_traffic,
        reasons=[],
    )
