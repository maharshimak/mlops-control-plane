from mlops_cp.drift import drift_severity, population_stability_index


def test_identical_distributions_are_stable() -> None:
    score = population_stability_index([50, 50], [50, 50])
    assert abs(score) < 1e-12
    assert drift_severity(score) == "stable"


def test_large_shift_is_detected() -> None:
    score = population_stability_index([90, 10], [20, 80])
    assert score > 0.25
    assert drift_severity(score) == "critical"
