from dataclasses import dataclass

from mlops_cp.models import ModelVersion


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    allowed: bool
    reasons: list[str]


def evaluate_promotion(
    model: ModelVersion,
    *,
    require_evaluations: int = 1,
) -> PromotionDecision:
    reasons: list[str] = []

    if len(model.evaluations) < require_evaluations:
        reasons.append(f"requires at least {require_evaluations} evaluation(s)")

    failed = [item.metric for item in model.evaluations if not item.passed]
    if failed:
        reasons.append("failed metrics: " + ", ".join(sorted(failed)))

    if not model.dataset_fingerprint:
        reasons.append("missing dataset fingerprint")

    return PromotionDecision(allowed=not reasons, reasons=reasons)
