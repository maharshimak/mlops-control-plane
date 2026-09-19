# Design and operating boundaries

In-memory model registry demonstrating evaluation gates, controlled promotion, dataset fingerprints and distribution drift.

## Scope

The API registry is process-local and loses state on restart. JSONL state, lineage and canary helpers are separate utilities, not deployment integrations. Artifact URIs are metadata; artifacts are not uploaded or verified. Registered models enter through gates, but library callers receive mutable model objects and are trusted. Thresholds are supplied by callers rather than an independent governance authority. No authentication, durable transactional registry, cloud deployment or automated rollback is implemented.

## Interfaces

Implementation lives in `src/mlops_cp/`. Public examples in the README use its Python API. FastAPI exposes the same local capabilities; `/openapi.json` is the endpoint schema.

## Validation

Tests include synthetic regression fixtures. Package and container checks verify installation separately from source-tree imports. Tests do not certify general model quality, clinical correctness or multi-tenant isolation.

## Planned evolution

Transactional persistence; immutable records; independent policy configuration; artifact checksums; authenticated approvals; deployment adapters.
