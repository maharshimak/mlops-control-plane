import math
import sys

import pytest

from mlops_cp.drift import drift_severity, population_stability_index


@pytest.mark.parametrize(
    "expected_scale,actual_scale",
    [(1, 1), (1e308, 1e308), (1e308, 1e-300), (1e-300, 1e308), (1e-300, 1e-300)],
)
def test_psi_is_invariant_to_independent_finite_count_scaling(expected_scale, actual_scale):
    reference = population_stability_index([1, 1], [1.7, 0.1])
    score = population_stability_index(
        [expected_scale, expected_scale],
        [1.7 * actual_scale, 0.1 * actual_scale],
    )

    assert math.isfinite(score)
    assert score == pytest.approx(reference, rel=1e-12)
    assert drift_severity(score) == "critical"


def test_extreme_finite_counts_preserve_zero_bin_drift():
    reference = population_stability_index([0, 1, 1], [1, 1, 0])
    score = population_stability_index([0, 1e308, 1e308], [1e308, 1e308, 0])
    assert score == pytest.approx(reference, rel=1e-12)
    assert drift_severity(score) == "critical"


def test_counts_at_largest_finite_float_stay_scale_invariant():
    largest = sys.float_info.max
    reference = population_stability_index([1, 1, 1], [1, 0.5, 0.25])
    score = population_stability_index(
        [largest, largest, largest], [largest, largest / 2, largest / 4]
    )
    assert score == pytest.approx(reference, rel=1e-12)


def test_identical_extreme_distributions_remain_stable():
    score = population_stability_index([1e308, 1e308], [1e308, 1e308])
    assert score == 0
    assert drift_severity(score) == "stable"


def test_smallest_positive_epsilon_keeps_zero_bin_psi_finite():
    epsilon = math.ulp(0.0)
    score = population_stability_index([0, 1], [1, 0], epsilon=epsilon)
    assert math.isfinite(score)
    assert score == pytest.approx(-2 * (1 - epsilon) * math.log(epsilon))


@pytest.mark.parametrize("invalid", [[0, 0], [-1, 1], [float("nan"), 1], [float("inf"), 1]])
def test_invalid_distributions_remain_rejected(invalid):
    with pytest.raises(ValueError):
        population_stability_index(invalid, [1, 1])
    with pytest.raises(ValueError):
        population_stability_index([1, 1], invalid)
