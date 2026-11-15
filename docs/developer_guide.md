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
| Small fix to released behavior | Commit directly to `master` | Push `master`, then merge `master` into `develop` |
| Large: a build spec with increments, or work that needs review before it integrates | `feature/<name>` from `develop` | Pull request to `develop` |
| Release | `release/<version>` from `develop` | Pull request to `master`, then merge `master` into `develop` |

A small change that is not ready for users stays behind a feature flag, so that each long-lived
branch stays releasable. In this library, the usual flag is a name that `pycordis/__init__.py`
does not export until the feature is complete.

`develop` has no branch protection, so a direct commit is pushed as it is, and `ci.yml` checks
it after the push. Run the [checks](#checks) before the push, and fix a CI failure on `develop`
at once, because `develop` is the source of each release.

`master` is protected: every CI check must pass on a commit before that commit can be pushed to
`master`. A commit reaches `master` through a pull request, or, for a small fix to released
behavior, through a branch that runs CI first:

```bash
git push origin HEAD:refs/heads/hotfix/<name>   # ci.yml runs on every branch
gh run watch --exit-status "$(gh run list --branch hotfix/<name> --limit 1 --json databaseId --jq '.[0].databaseId')"
git push origin master                          # the same commit now has passing checks
git push origin --delete hotfix/<name>
```

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

pycordis tracks one upstream revision of `cordiverse/cordis`. The README records it. An upstream
change is reviewed by behavior, not applied as a line diff. To move to a newer revision:

1. Clone upstream into a scratch directory outside this repository.
2. Generate the inventory of the new revision into `conformance/upstream.json` with
   `scripts/upstream_inventory.py`. The conformance check now lists every new, removed, or
   changed upstream item: a missing ledger entry, a stale ledger entry, or a stale hash.
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
   divergence ID if there is one, and the new hash. Never copy a hash without this review.
5. Update the tracked revision in `README.md` in the same commit as the last reviewed item.
6. Release a new version (see below).

## Release

pycordis uses SemVer `0.x`. A change to the public API increments the minor version; other
changes increment the patch version.

1. Create `release/<version>` from `develop`, and set `version` in `pyproject.toml` there.
2. Merge the branch into `master` through a pull request.
3. Tag the merge commit on `master` `vX.Y.Z`, and push the tag.
4. Merge `master` into `develop`.

Projects that depend on pycordis pin the tag (see [Getting Started](getting_started.md#install)).
