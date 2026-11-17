# Contributing

pycordis implements the programming model of the Cordis paper in Python. The core package of
[Cordis](https://github.com/cordiverse/cordis) (`packages/core`) is the reference implementation.
Most of the rules below protect two properties: pycordis keeps every guarantee of the
[semantic contract](architecture.md#semantic-contract), and each difference from upstream
behavior is intended, recorded, and tested. A change that breaks either property costs more than
it shows in a diff.

Read [Architecture](architecture.md) for the design principles and the divergence table, and
[Developer Guide](developer_guide.md) for the checks, the test-speed policy, and the release
procedure, before you start. Set up the environment with
[Getting Started](getting_started.md#set-up-a-development-environment).

## Project Rules

These rules decide every other choice.

- **Contract, then conformance**: code keeps every
  [semantic-contract](architecture.md#semantic-contract) property, and gives the upstream result
  in every upstream test scenario at the tracked revision that `README.md` records. Use a Python
  feature where it gives a clearer API or a stronger guarantee, and do not copy an upstream
  defect. Every intended difference has a row in the
  [divergence table](architecture.md#divergences-from-upstream) with the evidence its kind
  requires. Any other difference is a bug.
- **No runtime dependencies**: `src/` imports only the standard library. Put a tool that only
  tests use in the `dev` dependency group, and a tool that only docs use in the `docs` group. A
  runtime dependency needs a stated reason.
- **No event loop**: pycordis does not use `asyncio`. A scheduler that the host selects drives the
  lifecycle coroutines on one thread.
- **Host neutrality**: the library never imports, names, or assumes a host application or GUI
  toolkit. Express a host need as a generic contract: the scheduler protocol, thread-safe submit,
  read-only introspection.
- **Single owner thread**: each root context owns one thread, and every context, fiber, and core
  service method runs on it. A different thread reaches the context only by submitting work to
  its scheduler or completing a `Deferred`, per
  [Design Principles](architecture.md#design-principles).
- **Public API stability**: the public API is what `pycordis/__init__.py` exports, plus the
  public modules `pycordis.loader`, `pycordis.timer`, and `pycordis.hmr`. Under SemVer `0.x`, a
  breaking change to it increments the minor version. Internal code changes freely and
  its callers move with it, without compatibility wrappers.
- **Fail fast**: reject bad input with a specific exception, and keep the upstream message text
  where upstream supplies one. Read attributes directly on known types instead of probing with
  `hasattr` or `getattr` and falling back.

## Python Source

- Annotate every function. The package ships `py.typed`, and generated references use the
  annotations. Use PEP 695 generic syntax.
- Keep branching shallow. When the goal is lower complexity, prefer a named pattern (state
  machine, strategy, adapter, visitor, command) to a chain of extracted helpers. The fiber
  lifecycle is a state machine: keep its transitions in one place.
- Extract a method when it has one clear, namable responsibility that is reusable or testable on
  its own. Do not extract to satisfy a static-analysis warning, and do not split a function that
  the conformance ledger marks `mirror = true`.
- Keep allocation off paths that run often, such as an empty scheduler pump or a service lookup.
- Reach collaborators through injection (scheduler, clock, validator), not through globals or
  attribute digging.
- Log with `logging.getLogger(__name__)`. Never add a handler or set a level in library code; the
  host owns both.
- Where a component cannot keep its invariant, it declines the operation and logs it. A discarded
  disposer, a swallowed exception, or a skipped branch is its own defect: a guard that can discard
  input needs an `else`.
- Remove what a change makes dead, in the same change: functions, constants, comments, and
  branches.

## Bug Fixes

Start from the invariant, not from the input that exposed the fault. A fix that removes a symptom
without naming the invariant is incomplete.

1. State the invariant the code must keep, in one sentence.
2. Say why it broke: which component held knowledge it should not have, or which authority was
   bypassed, duplicated, or assumed.
3. List the sibling cases the same root cause reaches, and fix the class, not the instance.
4. Choose the enforcement point that makes the fault impossible to represent, not merely unlikely.
5. Name the contract property that the fault breaks, if any, and check whether upstream has the
   same fault. If it does, follow [Upstream Has the Same Fault](#upstream-has-the-same-fault).

!!! info "Patch, or refactor"
    Refactor rather than patch when any of these hold: you add a second condition to a branch you
    already patched once; the fix re-implements a rule another component owns; the fix lists known
    bad cases where an authority could be asked; the correct behavior depends on a collaborator the
    component cannot see; a comment asserts an invariant that nothing enforces.

Prove the fix with a test that fails against the original code and passes after it, and cover the
sibling cases there. Drive it through the public surface; a fault that only private state reaches
sits at the wrong enforcement point. Name a test for the behavior it protects. Search for the same
faulty pattern elsewhere and report it, even outside the change.

## Tests

**Grow coverage, not test count.** Every new or changed public behavior, branch, and error case gets
at least one test. No percentage target excuses a missing one.

- One behavior per test. Assert observable behavior, not implementation details, and do not assert
  the same fact twice. Test a rule once, where it is owned.
- Add coverage in this order: a `@pytest.mark.parametrize` case on an existing test; stronger
  assertions in an existing test; a new test function for a new behavior. Merge sibling tests that
  differ only in input values, including ones you did not just write.
- Use the public surface only. Documented read-only introspection is public surface; a `_private`
  attribute is not, even when it is readable. When only private state reaches a behavior, extract
  a testable collaborator.
- Keep mock patching out of tests. A test that patches more than one or two objects marks a unit
  with too many collaborators, which is a design fault. Prefer a small fake, such as a recording
  backend or a manual scheduler.
- Use pytest and the standard library. No network access, and write files only under `tmp_path`.
  Seed any randomness and parametrize over the seeds.
- Keep scheduling deterministic: advance the scheduler with explicit steps, and never wait on
  `time.sleep` or any other clock. Mark a test slow per
  [Developer Guide](developer_guide.md#slow-tests).
- A test that starts a thread joins it with a timeout and asserts that the join completed. Reach
  cross-thread behavior through scheduler submit and `Deferred` completion only.
- A test whose behavior is gone leaves in the same change.

!!! info "Traceability markers"
    A translated upstream case carries `@pytest.mark.upstream("<case ID>")`, a test of a contract
    property carries `@pytest.mark.contract("<ID>")`, and a test of a divergence carries
    `@pytest.mark.divergence("<ID>")`. Each ID is a string literal, because the conformance
    checks read the markers with `ast`. A symbol ID has the `src/` prefix
    (`src/fiber.ts:Fiber._unload`). The markers, not the test names, link a test to upstream,
    so translated cases follow normal consolidation: a merged test keeps one marker per case.

## Documentation

- Docstrings are the reference documentation: the API reference builds from them. Use Google style,
  and give every public module, class, function, and method one.
- Write a docstring for a Python developer who uses pycordis and has not seen upstream: what the
  object is for, where it fits in the system, the contract of its interface, and an example. A
  docstring never names upstream, TypeScript, or the port; the conformance ledger holds that
  mapping.
- State the present. A docstring, comment, test name, or guide says what the code does and why,
  never what it did before, which change introduced it, or which plan asked for it. A comment can
  name a contract ID or a divergence ID; it never names an upstream revision or line number.
- Give each fact one home. The table in the
  [documentation guide](README.md#contributing-to-documentation) says which page each kind of fact
  belongs to. Link rather than copy a signature.
- Write guides in ASD-STE100 Simplified Technical English: literal single-meaning words, no
  idioms, metaphors, or borrowed jargon, and no marketing or narrative phrasing. Write docstrings
  in plain, literal English, where a clear meaning is more important than the STE word rules.

## Porting From Upstream

- The [semantic contract](architecture.md#semantic-contract) decides what is correct. Upstream
  behavior at the tracked revision decides the result where the contract permits more than one.
  Python design decides the shape of the code and the API inside those limits.
- Take the observable result of each upstream test scenario, the state names, the event names,
  and the error message text. A test that checks a message checks the upstream text.
- Copy upstream control flow only for the items that the conformance ledger marks
  `mirror = true`: the fiber state machine and the epoch digest. Elsewhere, write the clearest
  Python that gives the same results.
- Read the upstream file before you change the behavior it defines. Clone upstream into a scratch
  directory outside this repository and never add upstream files to it.
- Follow the established naming: `camelCase` members become `snake_case`, an upstream promise
  becomes `Deferred[T]`, and a symbol-keyed hook becomes a `cordis_`-prefixed attribute rather
  than a new dunder. Keep a leading underscore.
- Update the item's entry in `conformance/ledger.toml` in the same change. When the conformance
  check reports a stale hash, review the upstream change before you copy the new hash.

!!! warning "A new divergence needs agreement first"
    Open an issue before you implement a change in observable behavior or public surface that
    differs from upstream. Add its row (ID, kind, upstream behavior, pycordis behavior, contract,
    evidence) in the same change as the code. When an upstream change removes the reason for a
    divergence, remove the divergence and its row.

### Upstream Has the Same Fault

Do not fix it silently. Report it with a minimal reproduction in both implementations, and name
the contract property it breaks. The choice is the maintainer's: keep the upstream behavior and
wait for an upstream fix, or fix pycordis and record a `correction` divergence that links the
upstream issue or pull request.

## Pull Request Checklist

```bash
uv run pytest            # full suite
uv run pytest -m "not slow"   # fast loop, for iteration only
uv run ruff check .
uv run ruff format --check .
uv run python scripts/gen_ref_pages.py && uv run zensical build
```

Also confirm that:

- Every changed public behavior has a test, and no test asserts a fact another test covers.
- Docstrings and guide pages match the code, and no page cites a plan or a change.
- Any difference from upstream has a divergence row with its evidence, and the ledger entries
  of the changed items are current.
- No docstring names upstream, TypeScript, or the port.
- Nothing that the change made dead survives.
