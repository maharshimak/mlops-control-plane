import pytest

from mlops_cp.canary import CanarySnapshot, evaluate_canary


@pytest.mark.parametrize(
    "values",
    [
        (1.5, 0.01, 100, 0.9),
        (100, float("nan"), 100, 0.9),
        (100, 0.01, 0, 0.9),
        (100, 0.01, 100, 2),
    ],
)
def test_invalid_canary_snapshots_fail(values):
    with pytest.raises(ValueError):
        CanarySnapshot(*values)


def test_invalid_traffic_policy_fails():
    s = CanarySnapshot(200, 0.01, 100, 0.9)
    with pytest.raises(ValueError):
        evaluate_canary(s, s, -1)
