import pytest

from mlops_cp.deployments import DeploymentCommand, HTTPDeploymentTarget


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
