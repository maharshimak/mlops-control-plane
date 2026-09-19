from mlops_cp.governance import build_promotion_manifest, metric_regressions
from mlops_cp.models import Evaluation, ModelVersion


def _model(version: str, evaluations: list[Evaluation]) -> ModelVersion:
    return ModelVersion(
        name="fraud-model",
        version=version,
        artifact_uri=f"s3://models/{version}",
        dataset_fingerprint="dataset-v1",
        evaluations=evaluations,
    )


def test_manifest_is_stable_across_evaluation_order() -> None:
    first = [Evaluation("accuracy", 0.94, 0.9), Evaluation("latency", 80, 100, False)]
    second = list(reversed(first))
    assert build_promotion_manifest(_model("1", first)).fingerprint == build_promotion_manifest(
        _model("1", second)
    ).fingerprint


def test_metric_regressions_respect_directionality() -> None:
    baseline = _model("1", [Evaluation("accuracy", 0.95, 0.9), Evaluation("latency", 80, 100, False)])
    candidate = _model("2", [Evaluation("accuracy", 0.91, 0.9), Evaluation("latency", 120, 100, False)])
    regressions = metric_regressions(baseline, candidate)
    assert {item.metric for item in regressions} == {"accuracy", "latency"}
