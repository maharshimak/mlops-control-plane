import pytest

from mlops_cp.lifecycle import InvalidLifecycleTransition, validate_transition
from mlops_cp.models import Evaluation, ModelVersion
from mlops_cp.registry import ModelRegistry


def good_model(version="1"):
    model = ModelVersion(
        name="fraud",
        version=version,
        artifact_uri=f"file:///fraud-{version}.json",
        dataset_fingerprint="abc",
    )
    model.evaluations.append(
        Evaluation(metric="auc", value=0.9, threshold=0.8, higher_is_better=True)
    )
    return model


def test_registry_enforces_one_way_lifecycle():
    registry = ModelRegistry()
    model = good_model()
    registry.register(model)
    registry.promote_candidate("fraud", "1")
    registry.promote_production("fraud", "1")

    with pytest.raises(InvalidLifecycleTransition):
        registry.promote_candidate("fraud", "1")


def test_transition_graph_rejects_reactivation_of_archived_models():
    with pytest.raises(InvalidLifecycleTransition):
        validate_transition("archived", "candidate")
