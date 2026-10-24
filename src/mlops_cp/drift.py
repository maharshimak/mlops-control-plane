from math import fsum, isfinite, log


def population_stability_index(
    expected: list[float],
    actual: list[float],
    epsilon: float = 1e-6,
) -> float:
    """Compare bin proportions, preserving invariance to finite count scaling."""
    if len(expected) != len(actual) or not expected:
        raise ValueError("Expected and actual distributions must have equal non-zero length.")

    if not isfinite(epsilon) or epsilon <= 0:
        raise ValueError("epsilon must be finite and positive.")
    if any(not isfinite(v) or v < 0 for v in [*expected, *actual]):
        raise ValueError("Bin counts must be finite and non-negative.")
    expected_scale = max(expected)
    actual_scale = max(actual)
    if expected_scale <= 0 or actual_scale <= 0:
        raise ValueError("Distribution totals must be positive.")

    # Dividing by a positive common scale leaves each distribution unchanged,
    # while keeping totals bounded even when summing finite counts overflows.
    expected_scaled = [count / expected_scale for count in expected]
    actual_scaled = [count / actual_scale for count in actual]
    expected_total = fsum(expected_scaled)
    actual_total = fsum(actual_scaled)

    score = 0.0
    for expected_count, actual_count in zip(expected_scaled, actual_scaled):
        e = max(expected_count / expected_total, epsilon)
        a = max(actual_count / actual_total, epsilon)
        score += (a - e) * (log(a) - log(e))
    return score


def drift_severity(psi: float) -> str:
    if psi < 0.1:
        return "stable"
    if psi < 0.25:
        return "warning"
    return "critical"
