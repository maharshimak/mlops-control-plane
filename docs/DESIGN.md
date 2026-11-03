# Design and operating boundaries

Model registry demonstrating evaluation gates, controlled promotion, dataset fingerprints, distribution drift, artifact integrity checks and a hash-chained governance audit log.

## Scope

The API registry is SQLite-backed (`MLOPS_CP_DB_PATH`) and survives restart; the library registry is in-memory unless given a database path. JSONL state, lineage and canary helpers are separate utilities, not deployment integrations. Library callers receive mutable model objects and are trusted. The API uses a single shared bearer token (or loopback-only access when none is set); there are no per-user identities. No cloud deployment or automated infrastructure rollback is implemented: rollback and canary logic produce decisions only.

## Governance layer

`governed.GovernanceService` composes `ModelRegistry` with three stores that share the registry's SQLite file:

| Store | Write rule | Purpose |
| --- | --- | --- |
| `candidate_evidence` | insert-only (triggers) | Validated `candidate-evidence.v1` record per model version and its canonical SHA-256 |
| `approvals` | insert-only (triggers), one row per approver per model | Approvals bound to the evidence digest; submitter cannot approve |
| `audit_events` | append-only (triggers), hash-chained | Registration, artifact verification attempts, approvals and governed promotions |

Trust boundaries:

- Evidence digests (`artifact_sha256`, `dataset_fingerprint`, `config_sha256`, `code_version`) are producer claims. The control plane recomputes `config_sha256` when `config` is supplied, and the artifact digest when `verify_artifact` receives the bytes or a path confined to `MLOPS_CP_ARTIFACT_ROOT`.
- The promotion policy is server configuration. Requests can supply drift observations but cannot relax approvals, required metrics or drift thresholds.
- The audit chain is tamper-evident, not tamper-proof: no signatures or external anchoring are built in. Tail truncation is detected only against a caller-held `expected_head`.
- Writes across stores are not one transaction. Ordering makes partial failures fail closed (a model without evidence, or without a verification event, is denied).
- The older `/candidate` and `/production` REST routes keep their original basic gate; open PRs #12-#15 harden those registry paths. The governed policy is applied by `GovernanceService.promote_to_production` and reported by the `evaluate_promotion` contract operation.

## Interfaces

Implementation lives in `src/mlops_cp/`. Public examples in the README use its Python API. FastAPI exposes the same local capabilities; `/openapi.json` is the endpoint schema. `contracts.py` exposes the `makma.v1` envelope (`capabilities()`, `run_operation(...)`, `GET /v1/capabilities`, `POST /v1/ops/{operation}`); envelopes are validated in tests against `contracts/makma.v1.schema.json`.

## Validation

Tests include synthetic regression fixtures, adversarial audit-chain tampering, path-confinement checks and JSON Schema validation of real contract outputs. Package and container checks verify installation separately from source-tree imports. Tests do not certify general model quality, clinical correctness or multi-tenant isolation.

## Planned evolution

Single-transaction multi-store writes; signed artifacts and signed or externally anchored audit heads; per-user authenticated approvals; routing legacy promotion routes through the governed policy; deployment adapters behind explicit configuration.
