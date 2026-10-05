import pytest

from mlops_cp.deployments import DeploymentCommand, HTTPDeploymentTarget, command_for_rollout
from mlops_cp.models import ModelVersion
from mlops_cp.progressive import RolloutDecision


def test_deployment_command_is_typed_and_bounded():
    command = DeploymentCommand(
        model_key="fraud:2",
        artifact_uri="s3://models/fraud/2",
        action="set_traffic",
        traffic_percent=20,
        reason="canary passed",
    )
    assert command.traffic_percent == 20

    with pytest.raises(ValueError):
        DeploymentCommand(
            model_key="fraud:2",
            artifact_uri="s3://models/fraud/2",
            action="set_traffic",
            traffic_percent=101,
            reason="invalid",
        )


def test_remote_deployment_target_requires_https():
    with pytest.raises(ValueError, match="HTTPS"):
        HTTPDeploymentTarget("http://example.com/deploy")
    assert HTTPDeploymentTarget("http://localhost:9000/deploy").endpoint.startswith("http://")


def test_rollout_decision_translates_to_narrow_traffic_command():
    model = ModelVersion(
        name="fraud",
        version="2",
        artifact_uri="s3://models/fraud/2",
        dataset_fingerprint="dataset-v1",
        stage="candidate",
    )
    command = command_for_rollout(
        model,
        RolloutDecision("increase", 5, 25, ()),
    )
    assert command is not None
    assert command.action == "set_traffic"
    assert command.traffic_percent == 25


def test_hold_rollout_emits_no_deployment_command():
    model = ModelVersion(
        name="fraud",
        version="2",
        artifact_uri="s3://models/fraud/2",
        dataset_fingerprint="dataset-v1",
        stage="candidate",
    )
    assert command_for_rollout(model, RolloutDecision("hold", 5, 5, ("wait",))) is None
