# Build Spec: Step Reports

**Status**: ✅ Completed — spec 2 of 4 (see [Dependencies](#dependencies))

**Reference**: [#10 Feature request: hook that reports the duration and owner of each scheduler step](https://github.com/uclatommy/pycordis/issues/10)

## Goal

Let a host register a callback that receives the duration, the kind, the owner fiber, and the
effect label of each scheduler step, behind a feature flag, with no cost when no callback is
registered.

## Architecture

### What a step is

A step is one callable that a scheduler takes from its queue and runs. The report covers that
callable and all the work that it runs inline: listeners that it emits to, effect setups inside
`apply`, and the first step of another task that it starts with `start`. Work in a host call
(for example `ctx.use` with `ImmediateScheduler`) and timer callbacks that `ManualClock.advance`
runs outside a step are not steps, and get no report. Steps can nest only when a step calls
`FramePumpScheduler.pump`; the step runner saves and restores its per-step state around each
step, so an inner step does not change the report of the outer step.

### The report

```
class StepKind(Enum): START, RESUME, SUBMITTED

@dataclass(frozen=True, slots=True)
class StepReport:
    duration: float              # seconds, from the scheduler clock
    kind: StepKind
    fiber: Fiber | None
    label: str | None            # EffectMeta.label of the effect that owns the task
    error: BaseException | None  # the exception that leaves the step

observe_steps(scheduler: Scheduler, callback: Callable[[StepReport], None]) -> Callable[[], None]
```

`error` is the exception that propagates out of the queued callable. `_Task.run` catches every
`Exception` and rejects the task `Deferred`, so a failed task gives `error=None`; only a
`SUBMITTED` step, or a `BaseException` in a task, sets `error`. The `StepReport.error` docstring
says so.

The returned function removes the callback. All three names live in `pycordis.scheduler`, and
`pycordis/__init__.py` does not export them: this is the feature flag.

### How a step gets its owner

```mermaid
flowchart LR
    Q["queued callable"] --> T{"bound method<br/>of a _Task?"}
    T -->|"_Task.run"| S["START, task owner and label"]
    T -->|"_Task._resume"| R["RESUME, task owner and label"]
    T -->|no| U["SUBMITTED, no owner"]
    U --> C{"a timer callback<br/>claims the step?"}
    C -->|yes| UC["SUBMITTED, timer fiber and label"]
    C -->|no| UN["SUBMITTED, fiber None"]
```

- **Tasks**: `spawn` and `start` take an owner fiber and a label, and the `_Task` keeps them.
  The step runner identifies a task step from the queued bound method. It does this only while
  a callback is registered.
- **Timer callbacks**: `pycordis.timer._run_logged` is the one place where a user timer callback
  runs (`timeout`, `interval`, `throttle`, `debounce`). It calls a private
  `Scheduler._claim_step(fiber: Fiber, label: str) -> None` on `fiber.ctx.scheduler`, with the
  label that it already receives (the same text as the `EffectMeta.label` of the timer effect).
  The claim does nothing when no callback is registered or no step runs, applies only to a
  `SUBMITTED` step, and the first claim in a step wins: a step that runs several timer
  callbacks (a `ManualClock.advance` inside a step) reports the first one. This covers
  `ThreadingClock` and any host clock that submits its callbacks, with no change to
  `Scheduler.submit`.
- **`sleep` and `ticks`**: their timer steps only complete a `Deferred` and report
  `fiber=None`. The task that they wake reports its own `RESUME` step with its owner.
- **Why not `submit(fn, *, fiber, label)`**: the #10 feedback proposed it. `Scheduler` is a public
  ABC that hosts subclass. If the library called `submit` with new keywords, a host subclass with
  `submit(self, fn)` would break, and `0.1.3` is a patch release. A public label for host
  submits can come with a minor release.

### The base step runner

Both built-in schedulers run each queued callable through one private helper in the base class.
The helper times the step, builds the report, and calls the callbacks, only when callbacks
exist. A host subclass of `Scheduler` that does not use the helper sends no reports. The
`observe_steps` docstring says so.

## File Structure

```
src/pycordis/
├── scheduler.py        # MODIFY: StepKind, StepReport, observe_steps, base step runner, task owner
├── fiber.py            # MODIFY: owner and label at each spawn and start
├── events.py           # MODIFY: owner at each start
├── reflect.py          # MODIFY: owner at each start
├── timer.py            # MODIFY: _run_logged claims the step
├── loader/_entry.py    # MODIFY: owner at each spawn and start, and in the `run` helper
├── loader/_tree.py     # MODIFY: owner at each `run` call
├── loader/_group.py    # MODIFY: owner at each `run` call
├── loader/_service.py  # MODIFY: owner at each `run` call
└── hmr/_service.py     # MODIFY: owner at each start
tests/
└── test_scheduler.py   # MODIFY: report cases
conformance/
└── ledger.toml         # MODIFY: notes on the mirror entries
docs/
└── architecture.md     # MODIFY: divergence row
```

## Implementation References

- **This repository**: `_Task`, `spawn`, and `start` (`scheduler.py`); the queue loops of
  `ImmediateScheduler._drain` and `FramePumpScheduler.pump`; the scheduler clock from
  [Pump Budget](pump_budget.md); `Scheduler.assert_owner` (the enforcement point for the
  observe-only rule); `pycordis.timer._run_logged` (the one path of a timer callback);
  `Hook.ctx` in `events.py` (the owner of a listener task); `EffectMeta.label` in `fiber.py`
  and the labels that `_log_failure`, `_logged`, and `_revert_on_failure` already receive;
  `test_an_empty_pump_is_fast_and_allocates_nothing` (the pattern for the no-cost test).
- **Upstream**: none. The scheduler has no upstream equivalent. The `host-introspection` row is
  the precedent for the divergence row.

## Key Constraints

- **No cost with no callback**: no clock read, no report object, and no inspection of the
  queued callable. A test checks that a counting clock is never called. The empty-pump
  allocation test passes with no change.
- **Owners**: a fiber task (load, unload, async effect, effect revert, logged disposer) is owned
  by its fiber; a listener task of `serial` or `parallel` by `hook.ctx.fiber`; a provider-release
  task of `reflect` by the fiber of the provider; loader and hmr tasks by the fiber of the
  loader or hmr service. The label is the `EffectMeta.label` where an effect owns the task;
  otherwise `None`. Two task sites do not have the owner or the label today: the async setup
  `spawn` calls in `Fiber._execute` (`_Runner` has no label) and the revert `spawn` in
  `EffectHandle.__call__` (`EffectHandle` has no fiber). Each gets the missing value from
  `Fiber.effect`.
- **Mirror code**: `Fiber._set_epoch`, `_reload`, and `_unload` are `mirror = true` ledger items.
  They get only an owner argument at their `start` calls. Their control flow does not change.
  Each ledger entry gets a `note` that says so.
- **Observe only**: while the callbacks of a report run, the step runner sets `_owner` to a
  sentinel that matches no thread, and restores it in a `finally`. `assert_owner` keeps its
  one comparison; its failure branch raises `CordisError("a step observer cannot call
  pycordis")` when the sentinel is set, and `WRONG_THREAD` otherwise. The success path costs
  nothing extra. Every context, fiber, and core-service method calls `assert_owner` (directly
  or through `owner_thread`), so one check covers all of them, and `pending` and
  `fiber.get_effects()` also raise. The unguarded reads (`fiber.name`, `fiber.uid`,
  `fiber.state`) and `Scheduler.submit` stay allowed; `ImmediateScheduler.submit` from an
  observer only queues, because the sentinel fails its owner comparison. The `observe_steps`
  docstring lists what a callback can read. Reason: lifecycle work that a timing report starts
  would make the result depend on timing.
- **Callback errors**: an `Exception` from a callback is logged with
  `logging.getLogger(__name__)` (`pycordis.scheduler`). It does not reach the step, and the other
  callbacks and the later steps run.
- **A step that raises**: its report is sent first, with `error` set, and then the exception
  propagates as today. This answers point 2 of the #10 feedback.
- **Callback order**: in registration order. A callback that is added or removed during a report
  takes effect from the next step.
- **References**: the report holds the `Fiber`. The `observe_steps` docstring says that a
  callback must not keep a report after it returns.
- **Type hints**: `scheduler.py` imports `Fiber` only under `TYPE_CHECKING`, so the rule that the
  Python-runtime modules import no core module holds at run time.
- **Experimental warning**: the `observe_steps` docstring has a `!!! warning "Experimental"`
  admonition. It says that the API can change before it is exported from `pycordis`.

## Integration

- `pycordis/__init__.py` does not change (the feature flag). `pycordis.scheduler` is not a public
  module, so the new names have no stability promise until they are exported.
- Promotion (export from `pycordis`, a user guide section, and a public label for host
  `submit` calls) is a separate change after hosts try the flag.
- Release: a patch increment, in `0.1.3` with [Pump Budget](pump_budget.md). The work goes on
  `feature/step-reports` from `develop`.
- `conformance/ledger.toml`: a `note` on `src/fiber.ts:Fiber._setEpoch`, `Fiber._reload`, and
  `Fiber._unload`. No status or hash changes.

## Testing

- ✅ Kinds: a queued task first step gives `START`, a resume gives `RESUME`, and a host `submit`
  gives `SUBMITTED` with `fiber=None` (one parametrized test)
- ✅ An async effect whose step advances a `ManualClock` by 20 ms (the scheduler gets
  `clock=manual.now`) gives a report with a duration of 20 ms, the fiber, and the effect label;
  a synchronous effect gives a report with the fiber and no label. The `FakeClock` of
  `conftest.py` counts integer milliseconds and is not a scheduler clock
- ✅ A timer callback through `ThreadingClock` gives a `SUBMITTED` report with the fiber and the
  label of its timer effect (`ctx.interval()`); the test waits with `ImmediateScheduler.serve`,
  joins the clock with a timeout, and is marked `slow` if its call time is 0.1 s or more
- ✅ A submitted step that raises gives one report with `error` set, and then the exception
  reaches the caller of `pump`; a task that raises gives `error=None` and a rejected `Deferred`
  (add the report check to `test_pump_leaves_later_steps_queued_when_a_step_raises`)
- ✅ A callback that raises is logged (`caplog`), and the later steps run with no change to any
  fiber state
- ✅ `divergence("step-reports")`, `contract("LIFE-CONFLUENT")`: parametrized over seeds
  (seeded `random.Random`, seed in the assertion message), the same random order of `use`,
  `dispose`, and provider changes runs with and without a callback that calls a context method
  and records the `CordisError`; both runs reach the same fiber tree, providers, and effects.
  A callback that calls `submit` works, and one that reads `fiber.name` works
- ✅ With no callback, the `StepClock` of `test_scheduler.py` counts no reads; the empty-pump
  allocation test passes
- ✅ The disposer from `observe_steps` stops the reports; two callbacks receive reports in
  registration order
- ✅ Both `ImmediateScheduler` and `FramePumpScheduler` send reports: a module-level fixture in
  `test_scheduler.py`, parametrized over the two classes, gives the scheduler and a function
  that runs its queue. Do not parametrize the shared `run` fixture of `conftest.py`: every
  test module uses it
- ✅ `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .` pass; the
  unmarked slow tests section of the full suite is empty

## Documentation

- Docstrings: `StepKind`, `StepReport` (each field), and `observe_steps` (the experimental
  warning, what a step is, the observe-only rule, the report for a step that raises, the
  host-subclass limit, and an example with a threshold log). The module docstring of
  `pycordis.scheduler` names `observe_steps`. The API page `docs/api/scheduler.md` is generated.
- `docs/architecture.md`: the `step-reports` row (see [Appendix A](#appendix-a-divergences)).
- `docs/user_guide.md`: none. The feature is behind a flag, and the user guide documents only
  exported API. The section comes with the promotion.
- `zensical.toml`: none (no new page).

## Dependencies

- **Sequence**: spec 2 of 4. The order is [Pump Budget](pump_budget.md), Step Reports,
  [Checked Get](checked_get.md), [Context Restriction](../context_restriction.md).
- **Prerequisite**: [Pump Budget](pump_budget.md) (the scheduler clock in the base class).
- **Required by**: none.
- **Runtime**: none (standard library: `dataclasses`, `enum`, `logging`).
- **Dev**: none new.

## Migration Strategy

### Increment 1: Reports for each step
**Goal**: `observe_steps` gives a report for each step of both built-in schedulers, with the
kind, the duration, and the error, and with `fiber=None` and `label=None` for every step; the
observe-only rule, callback-error logging, and callback order are in place.
**Files**: `scheduler.py` (~130), `test_scheduler.py` (~150)
**Validation**: `uv run pytest tests/test_scheduler.py tests/test_threading.py`; manual: in
`uv run python`, observe a `FramePumpScheduler`, submit a step that raises, call `pump(1)`, and
check that the report has `error` set before the exception reaches the prompt
**Docs**: the docstrings of `StepKind`, `StepReport`, and `observe_steps`
**Status**: ✅ Completed
**Changed**:
- `scheduler.py`: `StepKind`, `StepReport`, and `observe_steps`; `Scheduler._run_step` times a step and sends the report; both built-in schedulers run each queued callable through it
- `scheduler.py`: `assert_owner` raises the observer message while the callbacks of a report run
- `test_scheduler.py`: report tests over both built-in schedulers
**Tests**:
- Added to `tests/test_scheduler.py`: kinds and durations, a step that raises, a callback that raises, disposer and order, observe-only rule, no clock read without a callback
- 97 passed: `tests/test_scheduler.py`, `tests/test_threading.py`, `tests/test_docstrings.py`; `ruff check` and `ruff format --check` are clean
**Deviations**:
- The raising-step case is a new test, not an addition to `test_pump_leaves_later_steps_queued_when_a_step_raises`, because it needs a task step and a callback
- The case "a callback that is added during a report" has no test: an observer cannot call `observe_steps`, so only the snapshot in `_report` implements it
- `StepKind` is set from the queued bound method in this increment, because `kind` is part of the increment goal
**Manual validation**:
- In `uv run python`, a `FramePumpScheduler` with `observe_steps(s, print)` printed the report (`SUBMITTED`, `error=RuntimeError('x')`) before the exception reached the caller of `pump(1)`

### Increment 2: Owners and labels
**Goal**: each task step reports its owner fiber and effect label, a timer callback claims its
`SUBMITTED` step, the seeded `LIFE-CONFLUENT` test passes, and the divergence row and ledger
notes are in place.
**Files**: `scheduler.py` (~30), `fiber.py` (~20), `events.py`, `reflect.py`, `timer.py`,
`loader/_entry.py`, `loader/_tree.py`, `loader/_group.py`, `loader/_service.py`,
`hmr/_service.py` (~5 each), `test_scheduler.py` (~120), `ledger.toml`, `architecture.md`
**Validation**: `uv run pytest`; manual: in `uv run python`, observe an `ImmediateScheduler`,
use a component with an async effect, and check the fiber and the label in the reports
**Docs**: `docs/architecture.md` `step-reports` row; `observe_steps` docstring (owners, the timer
claim, the example); review of every docstring in `scheduler.py`
**Status**: ✅ Completed
**Changed**:
- `scheduler.py`: `spawn` and `start` take keyword `fiber` and `label`; `_Task` keeps them; `_run_step` reads the owner from the queued bound method and keeps per-step state that it saves and restores; `Scheduler._claim_step` gives a `SUBMITTED` step an owner
- `fiber.py`: each `spawn` and `start` passes the fiber, and the label where an effect owns the task; `_Runner` has a `label`; `EffectHandle` takes its fiber
- `events.py`, `reflect.py`, `hmr/_service.py`, `loader/_entry.py`: each task has its owner; the loader `run` helper takes the owner fiber, and `_tree.py`, `_group.py`, `_service.py` pass it
- `timer.py`: `_run_logged` claims the step
- `ledger.toml`: a `note` on `_setEpoch`, `_reload`, and `_unload`
- `architecture.md`: the `step-reports` row
**Tests**:
- Added to `tests/test_scheduler.py`: task owner and label with a 20 ms duration, an inner step, a timer callback through `ThreadingClock`, a step that runs several timer callbacks, and the seeded `LIFE-CONFLUENT` test (30 seeds)
- 1407 passed in the full suite; `ruff check` and `ruff format --check` are clean; the timer test fails when the claim is removed
**Deviations**:
- A `serial` dispatch is one task for all its listeners, so its owner is the fiber of the first listener (`None` with no listener); a `parallel` gather task has no owner
- The seeded test imports its schedule helpers from `tests/test_invariants.py`
- The full suite ran at this increment to check regressions; the slow-test check waits for close-out

## Code Standards

- [Contributing](../../contributing.md): the test and docstring standards, with the pull request
  checklist
- [Developer Guide](../../developer_guide.md): branches, slow tests, and release rules
- [Architecture](../../architecture.md): the semantic contract (`LIFE-CONFLUENT`) and the
  divergence table
- The instruction files in `.github/instructions/`: Python Coding Standards (mirror items),
  Docstrings & Markdown Style, Testing Standards, and Upstream Porting (ledger notes)

This spec is scaffolding — do not cite it, its filename, or its increment numbers in source,
docstrings, comments, or test names. Docstrings state the present rule, not the change that
introduced it.

## Appendix A: Divergences

The maintainer approved this row on #10. It goes to the divergence table in
`docs/architecture.md` in Increment 2.

| ID | Kind | Upstream | pycordis | Contract | Reason and evidence |
|---|---|---|---|---|---|
| `step-reports` | improvement | No timing or owner information for scheduled work | `pycordis.scheduler.observe_steps` reports the duration, kind, owner fiber, effect label, and error of each step; a callback cannot call pycordis | `LIFE-CONFLUENT` | A host finds the plugin that makes a slow frame. The callback observes only, so the reports do not change a lifecycle result. Test: `divergence("step-reports")` in `tests/test_scheduler.py` |
