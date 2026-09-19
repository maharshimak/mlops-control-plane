import json
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite

from mlops_cp.models import ModelVersion


@dataclass(frozen=True, slots=True)
class PromotionManifest:
    model_key: str
    dataset_fingerprint: str
    artifact_uri: str
    stage: str
    evaluation_count: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class MetricRegression:
    metric: str
    baseline: float
    candidate: float
    delta: float


def build_promotion_manifest(model: ModelVersion) -> PromotionManifest:
    if not model.name.strip() or not model.version.strip():
        raise ValueError("model name and version are required")
    if not model.artifact_uri.strip() or not model.dataset_fingerprint.strip():
        raise ValueError("artifact URI and dataset fingerprint are required")

    evaluations = sorted(
        (
            item.metric,
            item.value,
            item.threshold,
            item.higher_is_better,
            item.passed,
        )
        for item in model.evaluations
    )
    payload = {
        "model_key": model.key,
        "dataset_fingerprint": model.dataset_fingerprint,
        "artifact_uri": model.artifact_uri,
        "stage": model.stage,
        "evaluations": evaluations,
    }
    fingerprint = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PromotionManifest(
        model_key=model.key,
        dataset_fingerprint=model.dataset_fingerprint,
        artifact_uri=model.artifact_uri,
        stage=model.stage,
        evaluation_count=len(model.evaluations),
        fingerprint=fingerprint,
    )


def metric_regressions(
    baseline: ModelVersion,
    candidate: ModelVersion,
    *,
    tolerance: float = 0.0,
) -> tuple[MetricRegression, ...]:
    if not isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and non-negative")

    baseline_metrics = {item.metric: item for item in baseline.evaluations}
    regressions: list[MetricRegression] = []
    for item in candidate.evaluations:
        previous = baseline_metrics.get(item.metric)
        if previous is None or previous.higher_is_better != item.higher_is_better:
            continue
        improvement = (
            item.value - previous.value
            if item.higher_is_better
            else previous.value - item.value
        )
        if improvement < -tolerance:
            regressions.append(
                MetricRegression(
                    metric=item.metric,
                    baseline=previous.value,
                    candidate=item.value,
                    delta=item.value - previous.value,
                )
            )
    return tuple(sorted(regressions, key=lambda item: item.metric))
