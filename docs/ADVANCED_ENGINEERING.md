# Advanced engineering: deployment rollback policy

`mlops_cp.rollback` converts canary/deployment telemetry into an explainable rollback decision.

It evaluates absolute error rate, error-rate increase relative to baseline and p95 latency ratio. A
minimum request count prevents reacting to tiny samples; before that threshold the result explicitly
reports insufficient evidence rather than automatically rolling back.

The decision object contains calculated rates and every triggering reason, making it suitable for an
audit log, deployment controller or human approval screen.
