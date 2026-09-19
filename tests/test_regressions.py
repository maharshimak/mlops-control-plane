import pytest

from mlops_cp.drift import population_stability_index
from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.registry import ModelRegistry


def test_cannot_register_directly_as_production():
    with pytest.raises(ValueError):
        ModelRegistry().register(
            ModelVersion("demo", "1", "local/model", "abc", stage="production")
        )


def test_recheck_gate_at_production_promotion():
    r = ModelRegistry()
    r.register(ModelVersion("demo", "1", "local/model", "abc"))
    r.add_evaluation("demo", "1", Evaluation("accuracy", 0.9, 0.8))
    assert r.promote_candidate("demo", "1").allowed
    r.add_evaluation("demo", "1", Evaluation("accuracy", 0.2, 0.8))
    with pytest.raises(ValueError):
        r.promote_production("demo", "1")


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_non_finite_metrics_fail_closed(value):
    assert not Evaluation("quality", value, 0.8).passed
    with pytest.raises(ValueError):
        population_stability_index([1, 2], [value, 2])
