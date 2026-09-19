from mlops_cp.rollback import DeploymentTelemetry, RollbackPolicy, decide_rollback


def test_rollback_decision_triggers_on_regressions() -> None:
    telemetry = DeploymentTelemetry(
        requests=1_000,
        errors=100,
        p95_latency_ms=300,
        baseline_error_rate=0.01,
        baseline_p95_latency_ms=100,
    )

    decision = decide_rollback(telemetry, RollbackPolicy())

    assert decision.rollback
    assert len(decision.reasons) == 3
    assert decision.error_rate == 0.1
    assert decision.latency_ratio == 3.0


def test_rollback_waits_for_minimum_evidence() -> None:
    telemetry = DeploymentTelemetry(
        requests=20,
        errors=10,
        p95_latency_ms=500,
        baseline_error_rate=0.01,
        baseline_p95_latency_ms=100,
    )

    decision = decide_rollback(telemetry, RollbackPolicy(min_requests=100))

    assert not decision.rollback
    assert decision.reasons[0].startswith("insufficient evidence")


def test_healthy_deployment_does_not_roll_back() -> None:
    telemetry = DeploymentTelemetry(
        requests=1_000,
        errors=10,
        p95_latency_ms=110,
        baseline_error_rate=0.01,
        baseline_p95_latency_ms=100,
    )

    assert not decide_rollback(telemetry).rollback
