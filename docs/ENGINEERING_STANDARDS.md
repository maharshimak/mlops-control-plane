# Engineering standards

This repository is maintained as a production-oriented AI engineering portfolio project. The goal is not maximum feature count; it is **clear behavior, inspectable decisions, reproducibility, and evidence that changes work**.

## Definition of done

A code change is complete when it has focused tests, passes Ruff, passes the full regression suite, builds as a wheel, builds as a container, and updates public documentation when behavior or limitations change.

## Design principles

1. **Deterministic before clever.** Prefer explicit rules, schemas, budgets, and auditable decisions for safety-critical or release-critical behavior.
2. **Bounded execution.** Expensive operations should expose limits on time, cost, rows, steps, memory, or workload where relevant.
3. **Secure defaults.** No committed secrets, unrestricted shell execution, silent destructive data access, or undocumented trust boundaries.
4. **Observable behavior.** Important runtime and release decisions should produce inspectable reasons, metrics, traces, or manifests.
5. **Small interfaces.** Keep modules composable and dependency-light; add infrastructure only when it creates a measurable engineering benefit.
6. **Truthful documentation.** READMEs describe implemented behavior and known limitations rather than aspirational claims.

## Pull-request expectations

PRs should explain the problem, solution, risks, rollback path, and validation evidence. New logic should include tests that fail for the old behavior and pass for the new behavior whenever practical.

## Dependency policy

Dependencies should be necessary, maintained, and version-bounded. Dependabot opens reviewable updates weekly. Dependency changes must still pass installation, tests, packaging, and container validation.

## Release discipline

Releases should be reproducible from a Git commit. Release notes should summarize behavior, compatibility, validation and rollback considerations. Security-sensitive changes should avoid exposing exploit details before a fix is available.
