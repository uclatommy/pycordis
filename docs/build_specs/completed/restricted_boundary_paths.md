# Build Spec: Restricted Boundary Paths

**Status**: ✅ Completed — both increments are done

**Reference**: [#20 Restricted context: paths that the boundary leaves open](https://github.com/uclatommy/pycordis/issues/20)

## Goal

Close the step-report path for all observers, and make an `Exposure` on a callable class give
a restricted component a view that it can call. The other open paths stay documented.

## Architecture

### Scope

The four paths in #20, as found on `feature/cross-plugin-leaks` after the merge of the 0.2.1
release. Each result is what a restricted listener or observer received:

| Path | Result today | Decision |
|---|---|---|
| `StepReport.fiber` from `observe_steps(ctx.scheduler, …)` | The real `Fiber` | Close: every report gives a read-only owner record |
| Callable object, no marker | The real object | Stays open (gap policy, trust model) |
| Callable object with `Exposure` | A view that cannot be called | Fix: the view is callable |
| `tuple`/`list`/`dict`/`set`/`frozenset` subclass with `Exposure` | `FiberView` through the view (works) | No change; add a test |
| Any other object, no marker | The real object | Stays open (gap policy, trust model) |

pycordis's own types do not use the open paths: `Located` is internal (`ctx.reflect` is
refused), and `EffectHandle` keeps its fiber private. The paths are reached only through host
types. The trust model applies: loaded plugins are trusted, and `restrict` is a guard, not a
sandbox.

### Step reports: a read-only owner

A report that holds a `Fiber` gives any observer the fiber's full authority after the callback
returns. The observe-only rule covers only the time while the callback runs. The report is the
enforcement point: when it holds no fiber, no observer, restricted or not, can keep one. No
scheduler view is necessary, and `ctx.scheduler` stays the real scheduler.

```
@dataclass(frozen=True, slots=True)
class StepOwner:
    name: str          # Fiber.name at the end of the step
    uid: int | None    # Fiber.uid at the end of the step; None when the step disposed it

StepReport.owner: StepOwner | None   # replaces StepReport.fiber
```

- The step runner keeps the fiber internally (`_Step.fiber`, `_Task.fiber`, `_claim_step`) and
  builds the `StepOwner` only when it builds a report, so there is no cost with no callback.
- `StepOwner` is in `scheduler.py` and has no reference to a fiber, so the Python-runtime rule
  (no core-module import at run time) holds.
- A host that needs the fiber finds it by `uid` in its own fiber tree (`fiber.children`).
- The observe-only rule (`assert_owner` sentinel) does not change: a callback still cannot
  start lifecycle work during a report.

### Callable exposed view

`Exposure.__call__` gives an `_ExposedView`, which has no `__call__`: Python reads protocol
methods from the class, so the view is not callable. The fix follows the pattern of
`TraceableView` and `CallableTraceableView`:

- `_CallableExposedView(_ExposedView)` adds `__call__`. `Exposure.__call__` selects it when
  `callable(value)` is true, so an object that is not callable still gives a view that is not
  callable.
- A call passes each argument through `receive` and gives the result through
  `_exit(ctx, target, "__call__", …)`. So `"__call__"` in `Exposure.hidden` makes the call raise
  `CordisError` before the object runs, and `"__call__"` in `Exposure.safe` gives the result
  unchanged, as for any other name.
- Hashing and `__class__` are those of `_ExposedView`. Equality needs a change:
  `_ExposedView.__eq__` checks `type(other) is not _ExposedView`, so two callable views of the
  same object for the same scope are not equal. Check `isinstance(other, _ExposedView)`.

### Paths that stay open

A callable object or a container subclass with no marker, and any other unmarked object, pass
unchanged. A generic rule fails the gap policy:

- Wrapping every callable object breaks `isinstance` and identity for every caller.
- Rebuilding a container subclass needs a constructor rule per type (a `NamedTuple` does not
  take an iterable), which is a list of known cases.

The host closes each path for one class with `Exposure`, which works for container subclasses
and, after this spec, for callable objects.

## File Structure

```
src/pycordis/
├── scheduler.py    # MODIFY: StepOwner; StepReport.owner; reports build the owner record
├── traceable.py    # MODIFY: _CallableExposedView; Exposure.__call__ selects it
└── context.py      # MODIFY: restrict warning (open paths)
tests/
├── test_scheduler.py   # MODIFY: owner record cases
└── test_restrict.py    # MODIFY: callable exposure; container subclass
docs/
└── architecture.md     # MODIFY: step-reports row
```

## Implementation References

- **This repository**: `Scheduler._run_step`, `_report`, and `_claim_step` (`scheduler.py`);
  `TraceableView` and `CallableTraceableView`, `_Shadow` and `_CallableShadow` (the callable
  subclass pattern); `_exit` and `_read_exit` (the exit rule for `hidden` and `safe`);
  `receive` (inbound arguments); the `ContextView` member table in `context.py` (`scheduler`
  stays `pass`); the probe cases in the [Scope](#scope) table.
- **Upstream**: none. `observe_steps`, `restrict`, and `Exposure` have no upstream
  equivalent. No ledger entry changes.

## Key Constraints

- **No cost with no callback**: no `StepOwner` is built when `observe_steps` has no callback;
  the empty-pump allocation test passes with no change.
- **Snapshot**: `StepOwner` is built at the end of the step, from `fiber.name` and `fiber.uid`.
  Its docstring says that `uid` is `None` when the fiber was disposed by the end of the step.
- **No fiber in a report**: no attribute of `StepReport` or `StepOwner` refers to a `Fiber`.
  The traceback of `StepReport.error` can reach a fiber through its frames. The `restrict`
  warning already puts the traceback of an exception outside the guard, so do not change the
  error.
- **Callable only when callable**: `callable(view)` is true only when `callable(value)` is
  true.
- **Release**: `pycordis/__init__.py` exports none of `observe_steps`, `StepReport`,
  `Exposure`, or `restrict`, and `pycordis.scheduler` is not a public module. The changes have
  no SemVer effect. The branch already needs `release/0.3.0` for the notification-errors
  change.

## Integration

- Public exports: none change.
- Branch: `feature/cross-plugin-leaks` (with the notification-errors work). One pull request to
  `develop` after this spec closes.
- `conformance/ledger.toml`: no change.
- #20: close it with the pull request; its claim that `Exposure` closes the callable path is
  true after this spec.

## Testing

- ✅ `tests/test_scheduler.py`: the existing report tests assert `report.owner` (name and uid)
  in place of `report.fiber`
- ✅ A task step of a fiber that runs after the fiber is disposed gives `owner.uid is None` and
  the fiber's name. Example: the leave task of a fiber resumes after an async effect teardown.
  The step that calls `dispose` does not give this result, because its owner is the caller
- ✅ `divergence("step-reports")`: no attribute of a report or its owner is a `Fiber`
  (strengthen an existing report test). This test is the only test of the rule: a restricted
  observer gets the same report, so `tests/test_restrict.py` does not test it again
- ✅ `divergence("context-restriction")`: one parametrized test over an `Exposure` on a callable
  class: the call gives a `FiberView` for a `Fiber` result; a `FiberView` argument reaches the
  object as the real `Fiber`; `"__call__"` in `hidden` raises `CordisError` and the object does
  not run; `"__call__"` in `safe` gives the result unchanged; two views of the same object for
  the same scope are equal
- ✅ An `Exposure` on a class that is not callable gives a view that is not callable (a case of
  the test above)
- ✅ An `Exposure` on a `NamedTuple` gives a `FiberView` for a `Fiber` field, by attribute and
  by index
- ✅ The callable-object test at "A callable object that is not a function form" keeps its
  result: an unmarked callable object passes unchanged
- ✅ Each new test fails against the code before the change (revert check)
- ✅ `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .` pass; the
  unmarked slow tests section of the full suite is empty

## Documentation

- Docstrings: `StepOwner` (each field and the snapshot rule); `StepReport` (`owner`; remove
  the sentence about the reference to its fiber, which is no longer true); `observe_steps` (the
  paragraph about which steps report a fiber names the owner instead; a callback reads the owner
  record; remove the list of fiber reads); `Exposure` (a callable class
  gives a callable view, and `"__call__"` in `hidden` or `safe`).
- `restrict` warning in `context.py`: remove the `observe_steps` bullet; correct the callable
  object text (the parenthetical now reads as if functions were the open path) so that it names
  "a callable object whose type is not a function, method, builtin, or `functools.partial`";
  say that an `Exposure` on such a class keeps it callable.
- `docs/architecture.md`: the `step-reports` row (see [Appendix A](#appendix-a-divergence)).
  The `context-restriction` row does not change: its gap sentence stays true.
- `docs/user_guide.md`: none. `observe_steps`, `restrict`, and `Exposure` are not exported.
- `zensical.toml`: none (no new page).

## Dependencies

- **Prerequisite**: the merge of `develop` (0.2.1 and 0.2.2) into `feature/cross-plugin-leaks`
  (done: `3e506a1`).
- **Related**: [Notification Listener Errors](notification_errors.md), on the same
  branch; no code dependency.
- **Runtime**: none.
- **Dev**: none new.

## Migration Strategy

### Increment 1: Read-only step owner
**Goal**: every `StepReport` gives a `StepOwner` in place of the fiber, and a restricted observer
gets no `Fiber`.
**Files**: `scheduler.py` (~30), `context.py` (~4), `test_scheduler.py` (~45),
`architecture.md` (1 row)
**Validation**: `uv run pytest tests/test_scheduler.py tests/test_restrict.py
tests/test_threading.py tests/test_timer.py tests/test_docstrings.py`; manual: in
`uv run python`, observe an `ImmediateScheduler`, use a component with an async effect, and
print `report.owner`
**Docs**: the docstrings of `StepOwner`, `StepReport`, and `observe_steps`; the `observe_steps`
bullet of the `restrict` warning; the `step-reports` row
**Status**: ✅ Completed

**Changed**:
- `scheduler.py`: `StepOwner(name, uid)`; `StepReport.owner` replaces `fiber`; `_run_step` builds the owner at the end of the step, only when an observer exists; docstrings of `StepOwner`, `StepReport`, and `observe_steps` give the owner record
- `context.py`: the `observe_steps` bullet is removed from the `restrict` warning
- `architecture.md`: the `step-reports` row

**Tests**:
- `tests/test_scheduler.py`: report tests assert `owner` and that it is read-only; new test for a disposed fiber (`uid is None`, name kept); `divergence("step-reports")` test asserts that no attribute of a report or its owner is a `Fiber`
- `uv run pytest` on test_scheduler, test_restrict, test_threading, test_timer, test_docstrings: 422 passed
- Full suite: 1732 passed; ruff check and format are clean

**Deviations**:
- None

**Manual validation**:
- An `ImmediateScheduler` observed a plugin with an async teardown: reports show `StepOwner(name='plugin', uid=1)` while the fiber is alive and `uid=None` after `dispose`; no report holds a `Fiber`

### Increment 2: Callable exposed view
**Goal**: an `Exposure` on a callable class gives a restricted component a callable view with
filtered arguments and result.
**Files**: `traceable.py` (~30), `context.py` (~6), `test_restrict.py` (~70)
**Validation**: `uv run pytest`; manual: in `uv run python`, emit a marked callable object that
returns a fiber to a restricted listener, and check that the call gives a `FiberView`
**Docs**: the `Exposure` docstring; the callable-object text of the `restrict` warning; a review
of every docstring in `traceable.py` that names `Exposure`
**Status**: ✅ Completed

**Changed**:
- `traceable.py`: `_CallableExposedView` adds `__call__` (arguments through `receive`, result through `_exit` with the name `"__call__"`); `Exposure.__call__` selects it when `callable(value)`; `_ExposedView.__eq__` uses `isinstance`; the `Exposure` docstring describes the callable view
- `context.py`: the callable-object text of the `restrict` warning names a callable object whose type is not a function, method, builtin, or `functools.partial`, and says that an `Exposure` keeps it callable

**Tests**:
- `tests/test_restrict.py`: four tests of an `Exposure` on a callable class (arguments by position and by keyword with a filtered result; `hidden` and `safe`; equality; not callable); parametrized test of an `Exposure` on a `NamedTuple` subclass (attribute and index)
- Revert check: with the `traceable.py` change stashed, 5 of the new cases fail (the equality test guards the `isinstance` check, and passes both ways)
- Full suite: 1740 passed; ruff check and format are clean; the unmarked slow tests section is empty; ruff check and format are clean

**Deviations**:
- The plan names one parametrized test for the callable view; the cases check different rules, so they are four tests with parametrization where only the input differs

**Manual validation**:
- In `uv run python`, a marked callable class gave a callable view: a `Fiber` result was a `FiberView`, a `FiberView` argument reached the object as the real `Fiber`, `"__call__"` in `hidden` raised `CordisError` before the object ran, `"__call__"` in `safe` gave the real result, and a class that is not callable gave a view that is not callable

## Code Standards

- Python Coding Standards (`.github/instructions/python-coding-standards.instructions.md`):
  enforcement point, no vestigial code (the report reference sentence)
- Docstrings & Markdown Style (`.github/instructions/docstrings-and-markdown.instructions.md`)
- Testing Standards (`.github/instructions/testing-standards.instructions.md`): parametrized
  cases, public surface only
- Upstream Porting (`.github/instructions/upstream-porting.instructions.md`): divergence
  evidence

This spec is scaffolding — do not cite it, its filename, or its increment numbers in source,
docstrings, comments, or test names. Docstrings state the present rule, not the change that
introduced it.

## Appendix A: Divergence

The maintainer approved the owner record on #20. The `step-reports` row in
`docs/architecture.md` changes in Increment 1:

| ID | Kind | Upstream | pycordis | Contract | Reason and evidence |
|---|---|---|---|---|---|
| `step-reports` | improvement | No timing or owner information for scheduled work | `pycordis.scheduler.observe_steps` reports the duration, kind, owner (fiber name and uid), effect label, and error of each step; a report holds no fiber, and a callback cannot call pycordis | `LIFE-CONFLUENT` | A host finds the plugin that makes a slow frame. The callback observes only, and the report gives no fiber to keep, so the reports do not change a lifecycle result. Test: `divergence("step-reports")` in `tests/test_scheduler.py` |
