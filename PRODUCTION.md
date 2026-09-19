# Production engineering

`mlops_cp.governance` adds immutable promotion manifests and baseline-aware metric regression detection.

A promotion manifest fingerprints the model identity, dataset fingerprint, artifact URI, stage and sorted evaluation evidence. This creates an auditable decision artifact that remains stable even when evaluation records arrive in a different order.

`metric_regressions` compares candidate metrics with a baseline while respecting whether higher or lower values are better.

## Operational practice

- Store promotion fingerprints with deployment records.
- Require dataset fingerprints and immutable artifact URIs.
- Compare candidates against the currently promoted model before stage changes.
- Keep approval identity and timestamps in the external control-plane audit log.
