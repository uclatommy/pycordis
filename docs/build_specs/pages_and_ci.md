# Build Spec: Pages and CI

**Status**: 🟢 Complete — all four increments published. `master` at the merge of
`feature/ci-tests-lint` runs `ci.yml` (six `test (…)` legs, `lint`, `docs`) and `pages.yml`, and
`https://uclatommy.github.io/pycordis/` serves the site

## Goal

Run the tests, lint, and a strict docs build on every push and pull request, and publish the
documentation site to GitHub Pages from `master` only.

## Architecture

### Two workflows, one publisher

The repository uses gitflow: `master` holds releases, `develop` holds integrated work, and
`feature/*`, `release/*`, and `hotfix/*` branches merge into them. The published site shows the
latest release, so only `master` publishes. At authoring time `origin` has `master` only, so the
first increment also creates `develop` with `git push origin origin/master:refs/heads/develop`.
`origin/main` is a stale branch outside this workflow; this spec does not delete it, and no
trigger names it.

`ci.yml` checks every branch and every pull request to `develop` or `master`. `pages.yml` is the
only workflow that has `pages: write`, and only a push to `master` triggers it. A `develop` or
feature branch runs `ci.yml` and never reaches a deploy job.

An in-repo pull request emits both events for its head commit, so `ci.yml` runs twice for that
commit. Duplicate runs are an accepted decision: the suite takes seconds, and a `branches-ignore`
filter on the `push` trigger would add a rule that must match the branch naming.
`pull_request` also fires when a pull request closes without merging, which starts a run that no
merge needs.

```mermaid
flowchart LR
    PR["pull_request to develop or master"] --> CI
    PUSH["push (any branch)"] --> CI
    MASTER["push to master"] --> PAGES
    subgraph CI["ci.yml — contents: read"]
        T[test] ~~~ L[lint] ~~~ D[docs: generate, drift check, strict build]
    end
    subgraph PAGES["pages.yml — contents: read"]
        B["build: generate, drift check, strict build, upload site/"] --> DEP["deploy: pages: write, id-token: write"]
    end
```

| Workflow | Triggers | Workflow `permissions` | Job `permissions` |
|---|---|---|---|
| `ci.yml` | `push` (all branches), `pull_request` with `branches: [develop, master]` | `contents: read` | none added |
| `pages.yml` | `push` with `branches: [master]` | `contents: read` | `deploy`: `pages: write`, `id-token: write` |

- `pages.yml` has no `workflow_dispatch` trigger, because a manual dispatch can select any
  branch. To run a failed publish again, re-run the run of the `master` push.
- `deploy-pages` gets an OIDC token, so the `deploy` job also needs `id-token: write`.
  `pages: write` alone is not sufficient.
- The `deploy` job sets `environment: github-pages`. When the Pages source is GitHub Actions, the
  environment's deployment-branch rule allows only `master`. This is a second guard after the
  trigger filter.
- `pages.yml` sets `concurrency: { group: pages, cancel-in-progress: false }`. A deploy that is
  running always finishes. `ci.yml` groups by `${{ github.workflow }}-${{ github.ref }}` and
  cancels a superseded run.

### The docs build is one gated sequence

The `docs` job in `ci.yml` and the `build` job in `pages.yml` run the same steps in this order:

1. `uv run python scripts/gen_ref_pages.py`
2. Drift check: `git diff --exit-code` and an empty `git status --porcelain`
3. `uv run zensical build --strict`

The generator rewrites the `API_NAV` and `BUILD_SPECS_NAV` sections of the tracked
`zensical.toml`. When a commit adds a module or a build spec and does not have the regenerated
nav, the generator changes `zensical.toml` and step 2 fails. `pages.yml` runs the same check
before the upload, so a direct push to `master` cannot publish a stale nav.

A later increment that adds a file under `docs/` updates `zensical.toml` in the same commit as
the file, and runs `uv run python scripts/gen_ref_pages.py` to confirm the tracked nav needs no
further change. That check is local only, and CI repeats it on the push.

`docs/api/<module>.md` pages stay generated build output. `.gitignore` ignores `docs/api/*`
except `index.md`, and `docs/README.md` documents them as "not committed". Each build makes the
pages again from `src/`, so a page cannot be stale. The nav entries that point to the pages are
in `zensical.toml`, which step 2 checks. Thus the requirement "a merge never publishes a stale
nav" applies to one tracked file, not to the pages.

!!! info "API stubs stay uncommitted"
    A stub contains only `::: <module>` and follows from the module path. To commit the stubs
    would add one more file to each module change and give no more protection than the
    `zensical.toml` check.

`--strict` makes every warning fail the build, and that includes the link warnings from
`[project.validation.links] not_found = "warn"`. The current tree builds with "No issues found".

### uv in CI

- Install: `astral-sh/setup-uv` with `version: "0.9.3"` (the uv that wrote `uv.lock` revision 3)
  and `python-version` set per job. The action sets `UV_PYTHON`, and uv gets that interpreter.
  `actions/setup-python` is not used.
- Sync: `uv sync --frozen`. `[tool.uv] default-groups` installs `dev` and `docs`. The `lint` job
  also runs `uv lock --check`, because `--frozen` does not compare `uv.lock` with
  `pyproject.toml`.
- Cache: `enable-cache: true` on `setup-uv`, keyed on `uv.lock`. The action prunes the cache
  after the job. The cache from `actions/setup-python` is not used: it does not know uv's cache
  layout.
- Python: the `test` job has a matrix of `"3.12"` (the floor that `requires-python = ">=3.12"`
  sets) and `"3.13"` (the newest version in the `pyproject.toml` classifiers) crossed with
  `os: [ubuntu-latest, windows-latest, macos-latest]`, with `fail-fast: false`. Hosts skew
  Windows- and macOS-flavored, so the suite is checked on all three runner OSes. `uv.lock` is a
  universal lock for `>=3.12`, so `uv sync --frozen` works on every leg: `pyyaml` and `watchdog`
  are the only packages that build native code, and the lock has a `win_amd64` wheel and a
  macOS `arm64` wheel for each, so no leg compiles from a source distribution. The leg display
  names become `test (<python-version>, <os>)`. The `lint`, `docs`, and `build` jobs run on
  `ubuntu-latest` and 3.12 only. Public repositories get hosted macOS and Windows minutes under
  the same free terms as Linux.

### Slow tests in CI

CI runs the full suite, `uv run pytest`, with no marker filter. The `tests/conftest.py` docstring
gives the reason: only a full-suite run gives an authoritative **unmarked slow tests** report.
`-m "not slow"` stays a local fast loop. The report is visible in the job log and is not a gate,
because the call time on a shared runner is not stable enough to fail a build at 0.1 s.

The threshold has three homes, and they must agree: `SLOW_THRESHOLD_S` in `tests/conftest.py`
(the value that applies), the `slow` marker text in `[tool.pytest.ini_options] markers` of
`pyproject.toml`, and the "Slow Tests" section of `docs/developer_guide.md` (the published
policy). This spec does not change the value.

### Pages wiring

- Artifact: `actions/upload-pages-artifact` with `path: site`, and the default artifact name
  `github-pages`. `actions/deploy-pages` reads that name by default, so neither job sets a name.
- Jobs: `build` uploads the artifact. `deploy` has `needs: build`, `environment: { name:
  github-pages, url: ${{ steps.deployment.outputs.page_url }} }`, and one `deploy-pages` step.
- `actions/configure-pages` is not used. `site_url` in `zensical.toml` gives the base URL, and
  the build reads nothing else from that action.
- `zensical.toml` gets `site_url = "https://uclatommy.github.io/pycordis/"`, so that canonical
  links and the sitemap use the project subpath.
- Enable the source with the API, not with the settings UI, because the API call can be
  repeated and verified:
  `gh api -X POST repos/uclatommy/pycordis/pages -f build_type=workflow`. Verify with
  `gh api repos/uclatommy/pycordis/pages --jq .build_type`, which returns `workflow`. At
  authoring time, `GET .../pages` returns 404 (Pages is not enabled).
- Verify the published site with `curl -fsS` (which fails on a non-2xx status) piped to
  `grep -F "pycordis is a Python port of the core package"`. That text is the first body sentence
  of `docs/README.md` (after the page title), and it is in the built `site/index.html`.

## File Structure

```
.github/workflows/
├── ci.yml          # new: test, lint, docs jobs
└── pages.yml       # new: build and deploy jobs, master only
zensical.toml       # site_url
docs/
├── README.md       # published URL; strict build command
└── developer_guide.md  # "Continuous Integration" section
```

## Implementation References

- Docs build order and the generated nav markers: `scripts/gen_ref_pages.py` (module docstring,
  `replace_section`) and the header comment of `zensical.toml`
- Nav drift check: `Script.replace_section` in `zensical` reads and writes the nav by marker and
  returns the input unchanged when `nav.md` does not exist, so the check needs no feature flag
- Local checks that CI repeats: "Checks" in `docs/developer_guide.md` and the "Pull Request
  Checklist" in `docs/contributing.md`
- Slow-test report: `pytest_runtest_logreport` and `pytest_terminal_summary` in
  `tests/conftest.py`
- Public and private paths: the managed block that `.github/bin/attach.sh` writes to
  `.git/info/exclude` (`/.github/*`, `!/.github/workflows/`, `/.vscode/`)

## Key Constraints

- `.github/workflows/` is in the public clone and no other `.github/` path is. The managed block
  that `.github/bin/attach.sh` writes to `.git/info/exclude` ignores `/.github/*` and un-ignores
  `!/.github/workflows/`. Do not change that block, `.git/info/exclude`, or `attach.sh`. The gate
  on a new workflow file is the tracked file that `git ls-files .github/workflows` lists, not
  `git check-ignore`: the rules in `.git/info/exclude` apply at add time only, so a path that is
  already tracked stays tracked even when an ignore rule names it.
- No CI step writes under `.github/instructions/`, `.github/skills/`, or `.vscode/`. Those paths
  are not in the public clone. A step also leaves no new untracked file: `git status --porcelain`
  lists an untracked file that no ignore rule matches, and the `docs` job runs that check after
  the generator, whatever the command that wrote the file.
- Line endings reach only the `docs` job and the Pages `build` job. Each runs on Linux with a
  clean checkout, so `git diff` compares tracked bytes. A Windows or macOS leg of the `test` job
  checks out with `core.autocrlf` behavior of the runner and therefore runs no byte comparison:
  the test matrix adds no `git status` or `git diff` step to `test`.
- Lint is `uv run ruff check .` and `uv run ruff format --check .`. `black` is not in the `dev`
  group, so no step runs it.
- No change to `src/` and no runtime dependency. CI tooling comes from GitHub Actions and the
  existing `dev` and `docs` groups. No step imports `asyncio`.
- Only `pages.yml` has `pages: write` or `id-token: write`, and only on its `deploy` job.
- Pin each action to a major version tag that you verify against its repository at
  implementation time. Do not copy versions from memory.
- After each edit under `docs/`, `uv run python scripts/gen_ref_pages.py && uv run zensical build
  --strict` reports no issues.
- Duplicate `ci.yml` runs for a pull request head commit stay (see Architecture). Do not add a
  `branches-ignore` filter.
- Before each push, run `git status --porcelain .github` and `git ls-files .github`. They report
  the working tree state the public clone will get, which is the state the "Testing" checklist
  asserts.

## Integration

- `docs/developer_guide.md`: a "Continuous Integration" section after "Checks". It states which
  workflow runs which check, that CI runs the full suite and the fast loop is local only, and
  that a nav drift fails the `docs` job and the fix is to run the generator and commit
  `zensical.toml`.
- `docs/README.md`: the published URL under "Build the Documentation", and `--strict` in the
  build command, so that a local build applies the same gate as CI.
- `zensical.toml`: `site_url`.
- No public API change and no release.

## Testing

- ✅ `ci.yml` passes on a feature branch push and on a pull request to `develop` (needs `develop`
  on `origin`, which Increment 1 creates)
- ✅ One CI run shows six green `test` legs (three OSes by two Python versions)
- ✅ A commit that adds a build spec and does not have the regenerated `zensical.toml` fails the
  `docs` job at the drift check
- ✅ A push to a branch other than `master` starts no `pages.yml` run
- ✅ `gh api repos/uclatommy/pycordis/pages --jq .build_type` returns `workflow`
- ✅ `https://uclatommy.github.io/pycordis/` returns 2xx and contains the home-page sentence
- ✅ `git status --porcelain` in the local clone shows nothing under `.github/` other than
  `.github/workflows/`

## Dependencies

- Required: none new. `pytest` and `ruff` (`dev`), `zensical`, `mkdocstrings[python]`, and
  `pymdown-extensions` (`docs`) are in the lock.
- CI only (not Python packages): `actions/checkout`, `astral-sh/setup-uv`,
  `actions/upload-pages-artifact`, `actions/deploy-pages`.

## Migration Strategy

Each increment pushes to the public repository. Do the push only after the user confirms the
increment's diff.

### Increment 1: CI tests and lint

**Goal**: `ci.yml` runs the full test suite on Python 3.12 and 3.13, and lint, on every push and
pull request.

**Files**: `.github/workflows/ci.yml` (~50). Also creates `develop` on `origin` with the push
command in Architecture, because a pull request needs a target branch.

**Validation**: `gh run watch --exit-status "$(gh run list --workflow ci.yml --branch <branch> --limit 1 --json databaseId --jq '.[0].databaseId')"`

**Status**: 🟡 Implemented — `ci.yml` on `feature/ci-tests-lint`; `gh run watch --exit-status` for
run 43665177180 (push event) reports success for `test (3.12)`, `test (3.13)`, and `lint`; the
pull-request leg of the Testing checklist awaits the PR to `develop`

**Changed**:

- `.github/workflows/ci.yml`: `test` job (3.12/3.13 matrix, `fail-fast: false`, full
  `uv run pytest`) and `lint` job (`uv lock --check`, `ruff check`, `ruff format --check`);
  `push` on all branches, `pull_request` to `develop`/`master`, workflow `permissions`
  `contents: read`, cancel-in-progress concurrency per `${{ github.workflow }}-${{ github.ref }}`

**Tests**:

- No new tests; the `test` job runs the existing suite. Local run of the job commands
  (`uv sync --frozen`, `uv lock --check`, `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`) passes on 3.12; the 3.13 leg runs in CI.

**Deviations**:

- `origin/develop` already exists at `origin/master`, so the branch-creating push in
  Architecture is not run.
- `astral-sh/setup-uv` publishes no moving major tag above `v7` (verified via its tags API), so
  it is pinned to the commit SHA its README documents for `v10.3.3`; `actions/checkout` uses the
  moving `v7` tag as planned.

### Increment 2: Strict docs gate with nav drift check

**Goal**: The `docs` job in `ci.yml` fails on a stale generated nav or on any docs build
warning, and the guides document the CI checks.

**Files**: `.github/workflows/ci.yml` (+~20), `zensical.toml` (+1), `docs/developer_guide.md`
(+~15), `docs/README.md` (+~4)

**Validation**: the `gh run watch --exit-status` command of Increment 1, for the newest `ci.yml`
run of the branch. It passes on the branch head, which proves the `docs` job works on a clean
tree. For the failure case, commit the `docs/` file without its regenerated `zensical.toml` and
run `uv run python scripts/gen_ref_pages.py` right after: `git status --porcelain` then lists
`zensical.toml`, which proves the drift check catches it.

**Status**: 🟢 Implemented — `gh run watch --exit-status` for run 43674068283 (push of
`f8781ad`) reports success for `test (3.12)`, `test (3.13)`, `lint`, and `docs`; the drift
check's failure case is proven locally by the generator simulation in **Tests**

**Changed**:

- `.github/workflows/ci.yml`: `docs` job on `ubuntu-latest` with Python 3.12: `uv sync --frozen`,
  the generator, the drift check, and `uv run zensical build --strict`
- `zensical.toml`: `site_url = "https://uclatommy.github.io/pycordis/"`, so canonical links and
  the sitemap use the project subpath
- `docs/developer_guide.md`: "Continuous Integration" section after "Checks": which workflow
  runs which job, the full-suite rule, and the drift-check failure and fix
- `docs/README.md`: the published URL under "Build the Documentation" and `--strict` in the
  build command

**Tests**:

- No new tests; the `docs` job validates the documentation build. Local run of the job command
  sequence (`uv sync --frozen`, generator, drift check, `uv run zensical build --strict`)
  reports "No issues found" on 3.12 with `zensical.toml` unchanged by the generator
- Local drift simulation: a `docs/build_specs/` file added without the regenerated nav makes
  the generator modify `zensical.toml`, and the check commands then exit nonzero

**Deviations**:

- The drift check implements "an empty `git status --porcelain`" as
  `test -z "$(git status --porcelain)"`, because `git status` has no `--exit-code` option

### Increment 3: Cross-platform test matrix

**Goal**: The `test` job runs the suite on `ubuntu-latest`, `windows-latest`, and `macos-latest`
for both Python versions, so a platform-dependent failure cannot reach `master`.

**Files**: `.github/workflows/ci.yml` (+1, ~1): add the matrix key
`os: [ubuntu-latest, windows-latest, macos-latest]` and change the `test` job's `runs-on` from
`ubuntu-latest` to `${{ matrix.os }}`. `lint` keeps the hard-coded `ubuntu-latest`. The `docs`
job is unchanged, and `pages.yml` does not exist yet at this point.

**Notes**: `fail-fast: false` already covers the whole matrix, so a failing leg never cancels a
leg. A leg that fails on a path separator or an encoding assumption is fixed in the code or the
test, not by dropping the OS. Each leg syncs the `dev` and `docs` groups, because
`[tool.uv] default-groups` sets both. The slow-test report stays informational on every OS: the
0.1 s threshold and the full-suite rule do not change here.

**Validation**: `gh run watch --exit-status` of Increment 1, for the newest `ci.yml` run of the
branch. Then `gh run view <run-id> --json jobs --jq '.jobs[].name'` lists six `test (…)` legs,
one per OS and Python version.

**Status**: 🟢 Implemented — `gh run watch --exit-status` for run 43675781929 (push of
`ad67227` to `feature/ci-tests-lint`) reports success, and its jobs list names the six
`test (<python-version>, <os>)` legs plus `lint` and `docs`; the push of the merge to `master`
then passed with the same six legs

**Changed**:

- `.github/workflows/ci.yml`: `test` job matrix gains `os: [ubuntu-latest, windows-latest,
  macos-latest]` and `runs-on` becomes `${{ matrix.os }}`, so the legs display as
  `test (<python-version>, <os>)`; the `lint` and `docs` jobs keep `ubuntu-latest`

**Tests**:

- No new tests; the six matrix legs run the existing suite. Local run of the job commands
  (`uv run pytest`) passes on the macOS host, and the parsed workflow shows the
  two-dimensional matrix with `fail-fast: false`

**Deviations**:

- None

**Manual validation**:

- Confirmed in an interactive `uv run python` session: parsing `.github/workflows/ci.yml` with
  `yaml.safe_load` shows `runs-on` `${{ matrix.os }}`, the `os` and `python-version` matrix
  values, `fail-fast: false`, and `lint` and `docs` on `ubuntu-latest`, i.e. six matrix legs

### Increment 4: Publish to GitHub Pages from master

**Goal**: A push to `master` builds the site and deploys it, and the published URL serves the home
page.

**Files**: `.github/workflows/pages.yml` (~50). One repository setting: the Pages source is
GitHub Actions (the `gh api -X POST` call above; it is outward-facing, so get confirmation
before you run it)

**Procedure**: this increment is last, so the feature branch holds every change in the spec and
nothing yet on it is merged. It publishes by merging the branch locally into `master` and pushing
`master` directly, because no branch protection rule exists yet (see Follow-ups). Later updates to
`master` follow the branch workflow in Architecture and reach Pages through their merge.

**Validation**: `curl -fsS https://uclatommy.github.io/pycordis/ | grep -F "pycordis is a Python port of the core package"`

**Status**: 🟢 Implemented — Pages source is GitHub Actions, and run 43675870626 (push of the
merge `03049e0` to `master`) reports success for `build` and `deploy`;
`curl -fsS https://uclatommy.github.io/pycordis/ | grep -F "pycordis is a Python port of the core
package"` matches, and the site answers HTTP 200

**Changed**:

- `.github/workflows/pages.yml`: `build` job (checkout, setup-uv pinned as in `ci.yml`,
  `uv sync --frozen`, the generate/drift-check/strict-build sequence identical to the `ci.yml`
  `docs` job, `actions/upload-pages-artifact@v5` with `path: site` and the default artifact
  name) and `deploy` job (`needs: build`, `environment: github-pages` with the
  `steps.deployment.outputs.page_url` URL, job `permissions` `pages: write` and
  `id-token: write`, `actions/deploy-pages@v5`); trigger `push` with `branches: [master]` and no
  `workflow_dispatch`; workflow `permissions` `contents: read`; concurrency `group: pages` with
  `cancel-in-progress: false`

**Tests**:

- No new tests; `pages.yml` is CI configuration. Local run of the `build` job command sequence
  (generator, drift check, `uv run zensical build --strict`) reports "No issues found" on 3.12
  with `zensical.toml` unchanged by the generator. Action tags verified against the actions'
  repositories: `actions/checkout@v7`, `actions/upload-pages-artifact@v5`,
  `actions/deploy-pages@v5` exist as moving major tags

**Deviations**:

- None

**Manual validation**:

- Confirmed in an interactive `uv run python` session before the push: parsing
  `.github/workflows/pages.yml` with `yaml.safe_load` shows the `push`/`branches: [master]`
  trigger with no `workflow_dispatch`; workflow `permissions` `contents: read`; `concurrency`
  `pages` with `cancel-in-progress: false`; `deploy` with `needs: build`, the `github-pages`
  environment, and only job-level `pages: write` and `id-token: write`; the upload step with
  `path: site` and no artifact name; and the `run` step sequence equal to the `ci.yml` `docs`
  job's
- After the push: `gh api repos/uclatommy/pycordis/pages --jq .build_type` returns `workflow`,
  no branch other than `master` has a `pages.yml` run, and the home-page `curl` command of
  **Validation** matches

## Code Standards

- [Contributing](../contributing.md) — documentation rules and the pull request checklist
- [Developer Guide](../developer_guide.md) — checks, slow tests, and the docs build
- Docstrings & Markdown Style — present tense, admonitions, and blank lines between list and
  metadata lines

This spec is scaffolding — do not cite it, its filename, or its increment numbers in source,
docstrings, comments, or test names. Docstrings state the present rule, not the change that
introduced it.

## Follow-ups

- Branch protection rules on `develop` and `master` that require the six `test (…)` legs plus
  `lint` and `docs`. A required status check is an exact name, so a rule written before the test
  matrix would name `test (3.12)` and `test (3.13)`, names that stop existing when the matrix
  adds `os`. Write the rule after the matrix, with `git push` and merge not exempt.
