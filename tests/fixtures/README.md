# Pipeline fixtures

Generated once on 2026-11-03 by running `mlops-production-pipeline` (checkout at
`95dc79c58011b7db102b7b548baafa8f264059c9`, with that repository's uncommitted work-in-progress
present; the `candidate-evaluation.v1` schema file was unchanged from its commit `ab83f6f`)
in-process with Python 3.12.10:

```python
run_operation("evaluate_candidate", {
    "records": records, "schema": {"features": ["x1", "x2"], "label": "y", "group": "customer"},
    "config": config, "code_version": "95dc79c58011b7db102b7b548baafa8f264059c9",
})["data"]["candidate"]
```

`records` are 200 synthetic rows: for `index` in `0..199`,
`x1 = ((index * 7919) % 1000) / 10`, `x2 = index % 3`, `y = 2*x1 - 0.5*x2 + 3`,
`customer = f"c{index // 2:03d}"`.

| File | `config` | Pipeline gate |
| --- | --- | --- |
| `pipeline_candidate_pass.json` | `{"model_name": "fraud-ridge", "seed": 3}` | allowed |
| `pipeline_candidate_gate_failed.json` | same plus `"max_feature_psi": 0.0` | rejected (PSI above 0) |
| `*.model.json` | artifact bytes of the same run (`ExperimentResult.artifact_bytes` from `run_experiment` with identical inputs); SHA-256 equals the export's `artifact_sha256` | n/a |

Exports were written with `json.dumps(..., indent=2, sort_keys=True)`; the artifact bytes are
written unchanged (`.gitattributes` marks them binary). Do not edit these files by hand.
