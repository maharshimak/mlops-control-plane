from __future__ import annotations

from dataclasses import dataclass

from mlops_cp.canary import CanaryDecision, CanarySnapshot, evaluate_canary


@dataclass(frozen=True, slots=True)
class RolloutPolicy:
    stages: tuple[int, ...] = (1, 5, 25, 50, 100)
    min_requests_per_stage: int = 100
    max_error_rate_increase: float = 0.01
    max_latency_increase_ms: float = 250.0
    max_quality_drop: float = 0.02

    def __post_init__(self) -> None:
        if not self.stages or self.stages[-1] != 100:
            raise ValueError("rollout stages must end at 100 percent")
        if tuple(sorted(set(self.stages))) != self.stages:
            raise ValueError("rollout stages must be unique and strictly increasing")
        if self.stages[0] < 1 or any(stage > 100 for stage in self.stages):
            raise ValueError("rollout stages must be between 1 and 100")
        if self.min_requests_per_stage < 1:
            raise ValueError("min_requests_per_stage must be positive")


@dataclass(frozen=True, slots=True)
class RolloutDecision:
    action: str
    current_traffic_percent: int
    next_traffic_percent: int
    reasons: tuple[str, ...]


def evaluate_progressive_rollout(
    production: CanarySnapshot,
    candidate: CanarySnapshot,
    current_traffic_percent: int,
    *,
    policy: RolloutPolicy | None = None,
) -> RolloutDecision:
    policy = policy or RolloutPolicy()
    if current_traffic_percent not in policy.stages and current_traffic_percent != 0:
        raise ValueError("current traffic must be zero or a configured rollout stage")

    if current_traffic_percent == 100:
        return RolloutDecision("complete", 100, 100, ())

    next_stage = next(stage for stage in policy.stages if stage > current_traffic_percent)
    decision: CanaryDecision = evaluate_canary(
        production,
        candidate,
        current_traffic_percent,
        min_requests=policy.min_requests_per_stage,
        max_error_rate_increase=policy.max_error_rate_increase,
        max_latency_increase_ms=policy.max_latency_increase_ms,
        max_quality_drop=policy.max_quality_drop,
        step_percent=max(1, next_stage - current_traffic_percent),
    )

    if decision.action == "rollback":
        return RolloutDecision(
            "rollback",
            current_traffic_percent,
            0,
            tuple(decision.reasons),
        )
    if decision.action == "hold":
        return RolloutDecision(
            "hold",
            current_traffic_percent,
            current_traffic_percent,
            tuple(decision.reasons),
        )
    return RolloutDecision(
        "promote" if next_stage == 100 else "increase",
        current_traffic_percent,
        next_stage,
        (),
    )
