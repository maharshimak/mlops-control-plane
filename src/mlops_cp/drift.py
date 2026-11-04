from math import fsum, isfinite, log


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
    expected_proportions = _proportions(expected)
    actual_proportions = _proportions(actual)
    score = 0.0
    for expected_fraction, actual_fraction in zip(expected_proportions, actual_proportions):
        e = max(expected_fraction, epsilon)
        a = max(actual_fraction, epsilon)
        score += (a - e) * (log(a) - log(e))
    return score


def _proportions(counts: list[float]) -> list[float]:
    peak = max(counts)
    if peak <= 0:
        raise ValueError("Distribution totals must be positive.")
    scaled = [count / peak for count in counts]
    total = fsum(scaled)
    return [count / total for count in scaled]


def drift_severity(psi: float) -> str:
    if psi < 0.1:
        return "stable"
    if psi < 0.25:
        return "warning"
    return "critical"
