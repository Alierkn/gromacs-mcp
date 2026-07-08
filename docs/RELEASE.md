# Release Checklist

Use this checklist for tagged releases.

## Before Opening a Release PR

1. Update the version in `pyproject.toml` and `src/gromacs_mcp/__init__.py`.
2. Move `CHANGELOG.md` entries from `[Unreleased]` into the release version.
3. Run:

   ```bash
   uv lock --check
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy
   uv run pytest --cov --cov-report=term-missing
   RUN_GROMACS_INTEGRATION=1 uv run pytest -m integration
   uv build
   uvx twine check dist/*
   ```

4. Push a branch and confirm GitHub Actions is green.

## Publishing

1. Merge the release PR into `main`.
2. Create and publish a GitHub release tagged `vX.Y.Z`.
3. The `release.yml` workflow builds the package and publishes to PyPI only for
   a published GitHub release event.

Manual `workflow_dispatch` runs build and metadata checks only; it does not
publish to PyPI.

## PyPI Trusted Publishing

PyPI must be configured with a Trusted Publisher:

- Owner/repo: `Alierkn/gromacs-mcp`
- Workflow: `release.yml`
- Environment: `pypi`
- Tag pattern: optional, recommended as `v*`
