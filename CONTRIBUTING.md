# Contributing

Thanks for helping. dbt-refmerge edits people's dbt models, so its rule is simple: anything it cannot
prove, it refuses. Changes are welcome when they keep that rule.

## Setup

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,integration]"
pre-commit install   # optional: ruff and mypy on commit
```

## Test lanes

| Lane | Command | Needs |
|---|---|---|
| unit (default) | `pytest -q` | nothing; a few seconds |
| fake dbt | `pytest -q -m fake_dbt` | runs `tests/fakes/fake_dbt.py` as a subprocess |
| warehouse | `pytest -q -m warehouse` | Postgres and `dbt-postgres` |
| everything, with coverage | `pytest -q -m "" --cov` | Postgres; fails under 100% |

Start Postgres for the warehouse lane with:

```sh
docker run --rm -d --name refmerge-pg -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=refmerge postgres:16
export REFMERGE_TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/refmerge
```

## Before opening a pull request

```sh
ruff format --check src tests scripts
ruff check src tests scripts
mypy src
pytest -q -m "" --cov        # 100% line and branch coverage is enforced in CI
```

## Conventions

- **Tests are plain pytest functions.** No classes, and no mocks or patched internals. Use real seams:
  `tmp_path` projects, the fake dbt, the `faults` audit-hook fixture for OS failures, and real Postgres.
- **Assert what users see:** exact reason codes, statuses, exit codes, JSON, and complete bytes for
  rewrites.
- **Bug fixes start with a test that fails** for the reported reason.
- **Never weaken a refusal** to make a test pass or to raise coverage. When unsure, refuse with a
  `ReasonCode`.
- **Coverage exclusions** follow `TEST_COVERAGE_PLAN.md` §3 and §7: platform and Python-version
  pragmas, plus a short list of documented invariant guards.
- **User-visible changes** (CLI flags, exit codes, JSON output, reason codes) update `docs/` and
  `CHANGELOG.md` in the same pull request.

## Releases

1. Bump `__version__` in `src/dbt_refmerge/__init__.py` and add a `# X.Y.Z (YYYY-MM-DD)` section to
   `CHANGELOG.md`.
2. Merge to `main`, then `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The release workflow builds, uploads to TestPyPI, and waits for approval on the `pypi` environment.
   Check the TestPyPI build, approve, and the workflow publishes to PyPI and creates the GitHub release.
