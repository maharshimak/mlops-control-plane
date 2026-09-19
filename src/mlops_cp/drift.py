from math import isfinite, log


def population_stability_index(
    expected: list[float],
    actual: list[float],
    epsilon: float = 1e-6,
) -> float:
    if len(expected) != len(actual) or not expected:
        raise ValueError("Expected and actual distributions must have equal non-zero length.")

    if not isfinite(epsilon) or epsilon <= 0:
        raise ValueError("epsilon must be finite and positive.")
    if any(not isfinite(v) or v < 0 for v in [*expected, *actual]):
        raise ValueError("Bin counts must be finite and non-negative.")
    expected_total = sum(expected)
    actual_total = sum(actual)
    if expected_total <= 0 or actual_total <= 0:
        raise ValueError("Distribution totals must be positive.")

    score = 0.0
    for expected_count, actual_count in zip(expected, actual):
        e = max(expected_count / expected_total, epsilon)
        a = max(actual_count / actual_total, epsilon)
        score += (a - e) * log(a / e)
    return score


def drift_severity(psi: float) -> str:
    if psi < 0.1:
        return "stable"
    if psi < 0.25:
        return "warning"
    return "critical"
