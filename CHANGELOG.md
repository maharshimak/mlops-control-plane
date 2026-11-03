# Changelog

All notable engineering changes to this repository are documented here.

The format follows the intent of [Keep a Changelog](https://keepachangelog.com/) and uses semantic versioning principles where releases are created.

## [Unreleased]

### Governance
- Added `candidate-evidence.v1` schema for pipeline-produced candidate evaluations.
- `register_candidate` accepts `mlops-production-pipeline` `mlops-candidate-evaluation.v1` exports, validated against the vendored schema; a failed pipeline gate blocks promotion. `jsonschema` is now a runtime dependency.
- Added SHA-256 artifact verification with artifact-root path confinement.
- Added append-only, hash-chained audit log with tamper detection.
- Added governed promotion policy (artifact verified, gates, required metrics, drift, approvals) reporting every failing reason; `requires_approval` when only approvals are missing.
- Added `makma.v1` contract (`register_candidate`, `verify_artifact`, `evaluate_promotion`, `rollback_decision`, `audit_log`) and `/v1/capabilities`, `/v1/ops/{operation}` and approval routes.

### Fixed
- `POST /v1/models/{name}/{version}/candidate` on a model already past `registered` raised an unhandled `InvalidLifecycleTransition` (HTTP 500); it now returns 409.
- Model names/versions containing `:` (or blank) were accepted although registry keys are `name:version`, so `a:b`/`c` and `a`/`b:c` collided; the API now rejects them with 422.

### Engineering
- Added repository ownership and structured contribution workflows.
- Added Dependabot coverage for Python and GitHub Actions dependencies.
- Added issue and pull-request templates focused on reproducibility, validation, risk, and rollback.
- Added consistent editor configuration and engineering standards.

## Release policy

A release should describe user-visible behavior, compatibility notes, validation evidence, and any migration or rollback requirements. Repository history remains the source of truth for individual commits.
