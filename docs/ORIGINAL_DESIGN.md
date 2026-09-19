# Design notes

The control plane separates model metadata from training code.

A training job should produce an immutable model artifact and dataset fingerprint. The control plane receives that metadata, stores evaluations, and decides whether the artifact can move through lifecycle stages.

## Stage rules

- registered: artifact exists but has not passed gates
- candidate: all required gates have passed
- production: active production version
- archived: superseded production model

Promoting a new production version automatically archives the previous production version with the same model name.

## Production extension

The in-memory registry can be replaced with a database or MLflow adapter while keeping policy logic independent of storage.
