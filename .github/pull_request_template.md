## Summary

Describe the engineering problem and the solution in a few precise sentences.

## Change type

- [ ] Feature
- [ ] Bug fix
- [ ] Refactor
- [ ] Performance / reliability
- [ ] Security / governance
- [ ] Documentation / developer experience

## Validation

- [ ] `python -m ruff check .`
- [ ] `python -m pytest -q`
- [ ] `python -m pip wheel --no-deps . -w dist`
- [ ] `docker build -t mlops-control-plane:pr .`
- [ ] New behavior is covered by focused tests
- [ ] Public docs match the actual implemented behavior

## Risk and rollback

State compatibility risks, data/security implications, and the simplest rollback path. Write `None` when not applicable.

## Evidence

Add concise logs, metrics, screenshots, or examples when they materially help review.
