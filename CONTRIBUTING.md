# Contributing

Use Python 3.12+. Install `python -m pip install -e ".[dev]"`, then run `python -m ruff check .` and `python -m pytest -q`. Build the wheel with `python -m pip wheel --no-deps . -w dist`.

Keep pull requests focused. Describe behavior changes and limits, include regression tests for fixes, and use only synthetic fixtures. Offline tests must not depend on credentials or running model servers. Document optional integrations separately from implemented defaults.
