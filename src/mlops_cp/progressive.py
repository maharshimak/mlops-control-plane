from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from mlops_cp.canary import CanaryDecision, CanarySnapshot, evaluate_canary
from mlops_cp.governance import metric_regressions
from mlops_cp.models import ModelVersion
from mlops_cp.policy import evaluate_promotion


@dataclass(frozen=True, slots=True)
class RolloutPolicy:
    stages: tuple[int, ...] = (1, 5, 25, 50, 100)
    min_requests_per_stage: int = 100
    max_error_rate_increase: float = 0.01
    max_latency_increase_ms: float = 250.0
    max_quality_drop: float = 0.02

    def __post_init__(self) -> None:
        if not self.stages or any(type(stage) is not int for stage in self.stages):
            raise ValueError("rollout stages must be integer percentages")
        if self.stages[-1] != 100:
            raise ValueError("rollout stages must end at 100 percent")
        if tuple(sorted(set(self.stages))) != self.stages:
            raise ValueError("rollout stages must be unique and strictly increasing")
        if self.stages[0] < 1 or any(stage > 100 for stage in self.stages):
            raise ValueError("rollout stages must be between 1 and 100")
        if type(self.min_requests_per_stage) is not int or self.min_requests_per_stage < 1:
            raise ValueError("min_requests_per_stage must be a positive integer")
        for value in (
            self.max_error_rate_increase,
            self.max_latency_increase_ms,
            self.max_quality_drop,
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value < 0
            ):
                raise ValueError("rollout regression budgets must be finite and non-negative")


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


def evaluate_governed_rollout(
    production_snapshot: CanarySnapshot,
    candidate_snapshot: CanarySnapshot,
    current_traffic_percent: int,
    *,
    baseline_model: ModelVersion,
    candidate_model: ModelVersion,
    policy: RolloutPolicy | None = None,
    regression_tolerance: float = 0.0,
) -> RolloutDecision:
    """Combine offline model governance with live canary evidence."""
    promotion = evaluate_promotion(candidate_model)
    reasons: list[str] = []
    if not promotion.allowed:
        reasons.extend(f"promotion policy: {reason}" for reason in promotion.reasons)

    regressions = metric_regressions(
        baseline_model,
        candidate_model,
        tolerance=regression_tolerance,
    )
    reasons.extend(f"offline regression: {item.metric}" for item in regressions)

    if reasons:
        action = "rollback" if current_traffic_percent > 0 else "hold"
        return RolloutDecision(
            action=action,
            current_traffic_percent=current_traffic_percent,
            next_traffic_percent=0 if action == "rollback" else current_traffic_percent,
            reasons=tuple(reasons),
        )

    return evaluate_progressive_rollout(
        production_snapshot,
        candidate_snapshot,
        current_traffic_percent,
        policy=policy,
    )
