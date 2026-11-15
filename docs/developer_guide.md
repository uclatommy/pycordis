# Developer Guide

## Workflow

Work that has more than one step starts as a build spec in `docs/build_specs/`. A build spec
divides the work into increments. Each increment delivers one working feature with its tests,
and the maintainer confirms it before the next increment starts. When all increments are
complete, the spec moves to `docs/build_specs/completed/`.

This guide holds the mechanics: workflow, checks, test speed, porting steps, and releases.
[Contributing](contributing.md) holds the standards a change must meet, and
[Architecture](architecture.md) holds the design principles and the divergence table.

## Checks

Run these before each commit:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The fast loop excludes slow tests:

```bash
uv run pytest -m "not slow"
```

## Continuous Integration

The `ci.yml` workflow runs on every push and on pull requests to `develop` or `master`:

- `test`: the full suite with `uv run pytest`, on Python 3.12 and 3.13. CI never filters
  markers; the fast loop above is local only.
- `lint`: `uv lock --check`, `uv run ruff check .`, and `uv run ruff format --check .`
- `docs`: `uv run python scripts/gen_ref_pages.py`, a drift check, then
  `uv run zensical build --strict`. The drift check fails when the generator changes a
  tracked file, which means a module or build spec was added without its regenerated nav.

When `docs` fails at the drift check, run `uv run python scripts/gen_ref_pages.py` and commit
the updated `zensical.toml`. `--strict` fails the build on any warning, including a link that
does not resolve.

## Slow Tests

A test whose `call` phase takes 0.1 s or more gets `@pytest.mark.slow`. The test suite lists
each such test without the marker in the **unmarked slow tests** section of the pytest summary.
Only a full-suite run is authoritative. In a subset, a one-time import cost goes to the first
test that runs, so a fast test can show as slow.

## Port an Upstream Change

pycordis tracks one upstream revision of `cordiverse/cordis`. The README records it. To move to a
newer revision:

1. Clone upstream into a scratch directory outside this repository.
2. Diff `packages/core/src` and `packages/core/tests` between the tracked revision and the new
   one.
3. For each change, port the source change and its test change together. A change that the
   port cannot follow becomes a row in the
   [divergence table](architecture.md#divergences-from-upstream).
4. Update the tracked revision in `README.md` in the same commit as the last ported change.
5. Release a new version (see below).

Steps 1-4 apply one upstream change and its tests together; a change that the port cannot
follow becomes a divergence row before the tracked revision moves.

## Release

pycordis uses SemVer `0.x`. A change to the public API increments the minor version; other
changes increment the patch version.

1. Set `version` in `pyproject.toml`.
2. Commit, then tag the commit `vX.Y.Z`.
3. Push the commit and the tag.

Projects that depend on pycordis pin the tag (see [Getting Started](getting_started.md#install)).
