# Design and operating boundaries

The project is a compact model-lifecycle control plane for registration, evaluation gates,
promotion, drift checks and progressive rollout decisions.

## Scope

The FastAPI service uses a SQLite-backed `ModelRegistry` by default. Registered model
versions, evaluation evidence, current lifecycle stage and append-only lifecycle events
survive process restart.

Registration and lifecycle transitions are deliberately narrower than generic database
writes:

- model registration and its `registered` lifecycle event commit in one transaction;
- candidate promotion uses a compare-and-set stage update and records its lifecycle event
  in the same transaction;
- production promotion archives the previous production version, promotes the candidate
  and writes all associated lifecycle events atomically;
- a partial lifecycle transition is rolled back if its history event cannot be persisted.

The database also enforces at most one production row per model name. These guarantees
reduce crash and competing-writer failure modes around lifecycle state, but they do not
turn SQLite into a distributed registry. Evaluation appends are still a local
read/modify/write operation and should not be treated as a multi-region or high-contention
coordination mechanism.

Artifact URIs and dataset fingerprints are caller-supplied metadata. The service does not
upload artifacts, verify remote artifact contents or validate cryptographic signatures.

## Trust and authentication boundary

Library callers are trusted Python callers. The HTTP API is local-only when
`MLOPS_CP_API_TOKEN` is unset. Remote API access requires a bearer token.

The deployment adapter is a separate outbound boundary. `HTTPDeploymentTarget` accepts
only HTTP on loopback or HTTPS for remote endpoints, rejects credentials embedded in the
URL, validates finite positive timeouts and sends a typed deployment command rather than
shell code.

No cloud credentials, arbitrary command execution or infrastructure-specific deployment
logic are embedded in the policy engine.

## Lifecycle and governance

Lifecycle stages follow an explicit one-way graph:

`registered -> candidate -> production -> archived`

The promotion policy requires evaluation evidence and a dataset fingerprint. Candidate
and production decisions are deterministic from caller-provided evidence. Thresholds are
not supplied by an independent governance service, so passing the gate is evidence that
configured rules were satisfied, not independent certification of model quality.

Progressive rollout combines offline promotion/regression checks with live canary
snapshots. Rollout stages, sample requirements and regression budgets are validated before
use. Rollout decisions can be translated into narrow deployment commands, but the control
plane does not itself change real traffic unless a caller explicitly invokes a configured
deployment target.

## Interfaces

Implementation lives in `src/mlops_cp/`. Public examples in the README use the same
Python domain package exposed by FastAPI. Endpoint schemas are available through
`/openapi.json`.

The browser demo is maintained separately in the MAK'MA web product and mirrors selected
promotion/canary concepts; it is not the persistence or deployment authority for this
package.

## Validation

The test suite exercises lifecycle gates, SQLite persistence, rollback-on-history-failure,
governance manifests, rollout behavior and deployment command validation. CI runs Ruff,
pytest, wheel construction and a container build.

These checks verify implementation behavior. They do not certify general model quality,
security of downstream infrastructure, multi-tenant isolation or correctness of
caller-supplied datasets and metrics.

## Planned evolution

Highest-value remaining work includes:

- conflict-safe multi-writer evaluation evidence;
- independent policy and approval configuration;
- artifact digests/signatures tied to stored model metadata;
- authenticated human/service approvals;
- real deployment-controller adapters with idempotency keys and receipt persistence;
- OpenTelemetry/Prometheus integration and operational SLOs.
