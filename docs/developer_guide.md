# Developer Guide

## Workflow

Work that has more than one step starts as a build spec in `docs/build_specs/`. A build spec
divides the work into increments. Each increment delivers one working feature with its tests,
and the maintainer confirms it before the next increment starts. When all increments are
complete, the spec moves to `docs/build_specs/completed/`.

This guide holds the mechanics: workflow, branches, checks, test speed, porting steps, and
releases. [Contributing](contributing.md) holds the standards a change must meet, and
[Architecture](architecture.md) holds the design principles and the divergence table.

## Branches

The repository has two long-lived branches: `master` holds releases, and `develop` holds
integrated work. The size of a change decides how it reaches them:

| Change | Branch | How it merges |
|---|---|---|
| Small: a few commits that are complete alone, such as a documentation update, a bug fix, or a single-file change | Commit directly to `develop` | Push `develop` |
| Small fix to released behavior | `hotfix/<version>` from `master`, where `<version>` is the next patch version | Pull request to `master`, then merge `master` into `develop` |
| Large: a build spec with increments, or work that needs review before it integrates | `feature/<name>` from `develop` | Pull request to `develop` |
| Release | `release/<version>` from `develop` | Pull request to `master`, then merge `master` into `develop` |

A small change that is not ready for users stays behind a feature flag, so that each long-lived
branch stays releasable. In this library, the usual flag is a name that `pycordis/__init__.py`
does not export until the feature is complete.

`develop` has no branch protection, so a direct commit is pushed as it is, and `ci.yml` checks
it after the push. Run the [checks](#checks) before the push, and fix a CI failure on `develop`
at once, because `develop` is the source of each release.

`master` is protected. A commit reaches `master` only through a pull request from a
`release/<version>` or `hotfix/<version>` branch, and only after every CI check passes. Do not
push to `master`. Merge each pull request to `master` with a **merge commit**, because
GitVersion reads the version from the branch name in the merge message.

## Checks

Run these before each commit:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

To measure coverage (line and branch), add `--cov`. The run fails when total coverage is below
`fail_under` in `pyproject.toml`:

```bash
uv run pytest --cov --cov-report=term-missing
```

The fast loop excludes slow tests:

```bash
uv run pytest -m "not slow"
```

## Continuous Integration

The `ci.yml` workflow runs on every push and on pull requests to `develop` or `master`:

- `test`: the full suite with `uv run pytest --cov`, on Python 3.12, 3.13, and 3.14. CI never filters
  markers; the fast loop above is local only. The run fails when coverage is below
  `fail_under`. The Ubuntu, Python 3.12 job uploads `coverage.xml` to Codecov, which supplies
  the coverage badge in the README. The upload needs the `CODECOV_TOKEN` repository secret.
  `codecov.yml` sets the Codecov status checks: total coverage can drop by at most 1%, and the
  lines that a pull request changes need 90% coverage.
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

pycordis tracks one upstream revision of `cordiverse/cordis`. The README records it. An upstream
change is reviewed by behavior, not applied as a line diff. To move to a newer revision:

1. Clone upstream into a scratch directory outside this repository.
2. Set the new revision in `README.md`. Generate the inventory of that revision into
   `conformance/upstream.json` with `uv run python scripts/upstream_inventory.py <clone>`. Add
   `--ledger-stubs` to print `pending` ledger entries for the new items, and append them to
   `conformance/ledger.toml`. The conformance check now lists every new, removed, or changed
   upstream item: a missing ledger entry, a stale ledger entry, or a stale hash.
3. Review each listed item, and put it in one class:
    - **Contract change**: the change affects a semantic-contract property. Update the contract
      table first, then the tests, then the code.
    - **Behavior change**: the change gives a different result in a test scenario. Port the test
      first, then the change, unless it breaks a contract property (see
      [Contributing](contributing.md#upstream-has-the-same-fault)).
    - **Internal change**: the change gives the same results. For a `mirror = true` item, port
      the change with the same structure; otherwise no code change.
    - **TypeScript only**: types, build files, or JavaScript runtime details. No code change.
4. For each item, update its ledger entry: the status, the pycordis symbol or test, the
   divergence ID if there is one, and the new hash. Never copy a hash without this review: the
   hash records that the review took place. A translated test carries an `upstream` marker with
   the case ID as a string literal.
5. Add, change, or remove divergence rows in `docs/architecture.md` with the code. Delete a row
   when an upstream change removes its reason.
6. Release a new version (see below).

## Release

pycordis uses SemVer `0.x`. A change to the public API increments the minor version; other
changes increment the patch version. GitVersion calculates the version from the branch
structure, never from commit messages. `pyproject.toml` keeps the placeholder version `0.0.0`:
do not edit it, and do not create a version tag by hand.

1. Create `release/<version>` from `develop`. The branch name sets the version, so the name
   states the compatibility level that you decided.
2. Merge the branch into `master` through a pull request. Use a **merge commit**. A squash or
   rebase merge removes the branch name from the history, and `master` then gives only a patch
   increment of the last tag.
3. The merge starts `release.yml`. It calculates the version, publishes the build to PyPI, and
   then creates the tag `vX.Y.Z` on the built commit.
4. Merge `master` into `develop`.

A small fix to released behavior follows the same steps, from `hotfix/<version>` that you
create from `master`. The branch name sets the version, which is the next patch version.

`ci.yml` runs the same version calculation and build on every push and pull request (the
`build` job), so an error in `GitVersion.yml` fails CI before a release needs it. Only
`release.yml` uploads to PyPI, and it runs only on `master`.

If a release run fails, re-run the failed job:

- If `publish` fails and the cause is outside the repository (for example, the PyPI trusted
  publisher), no tag exists. Correct the cause, then re-run the workflow.
- If `publish` fails and the fix needs a change in the repository, commit the fix on the same
  `release/<version>` or `hotfix/<version>` branch, and merge it into `master` through a second
  pull request with a merge commit. Its merge message names the branch again, so the run
  calculates the same version. Before `publish` starts, check that the `build` job shows that
  version.
- If only `tag` fails, the upload is complete. Re-run **only** the failed job. A full re-run
  calculates the same version, and PyPI rejects a second upload of it.

Do not merge another pull request into `master` until the tag exists. The version of the next
merge depends on the tag.

### One-time setup

These steps cannot run in a workflow:

1. Register a pending trusted publisher on PyPI for the project `pycordis2` (repository
   `uclatommy/pycordis`, workflow `release.yml`, environment `pypi`). The distribution name
   is `pycordis2` because PyPI refuses `pycordis` as too similar to the project `py-cordis`.
   The import name stays `pycordis`.
2. Create the GitHub environment `pypi`.
3. Add the `build` job of `ci.yml` to the required checks of `master`.
4. Make sure that no tag ruleset blocks `GITHUB_TOKEN` from creating `v*` tags.
