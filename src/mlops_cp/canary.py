from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CanarySnapshot:
    requests: int
    error_rate: float
    p95_latency_ms: float
    quality_score: float


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
