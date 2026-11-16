# Build Spec: pycordis Core Port

**Status**: 🔄 In progress — Increment 6 of 12 complete

## Goal

Implement the Cordis programming model in Python as `pycordis` `v0.1.0`: keep every
semantic-contract property, give the upstream result in every upstream test scenario of
`packages/core`, use a scheduler model that needs no event loop, and give a public contract that
host applications can adopt.

## Why This

Python applications that load plugins at runtime (panels, tools, services, node libraries)
usually register into global tables that only append. Such registrations have no inverse, so a
plugin cannot be unloaded or reloaded without a process restart, and a provider that leaves
breaks its dependents. Cordis solves this with fibers that own revertible effects and declare
their dependencies, and its calculus proves recovery, ordering, progress, and confluence. A
Python implementation that keeps the
[semantic contract](../architecture.md#semantic-contract) gives Python hosts the same
guarantees, and the conformance ledger shows each upstream change that needs review.

## Architecture

### Keep the contract, conform to upstream, design for Python

Each upstream module in `packages/core/src` has one Python module with the same
responsibilities and state names. Behavior is correct when it keeps the contract properties and
gives the upstream result in each upstream test scenario. Only the fiber state machine and the
epoch digest copy the upstream control flow (`mirror = true` in the ledger); the other modules
use the clearest Python that gives the same results. Intended differences are the rows of
[Appendix A](#appendix-a-divergences), each with a kind. Any other difference in behavior is a
bug.

```mermaid
flowchart TB
    subgraph RT["Python runtime modules"]
        ERR[errors.py]
        DSP[disposables.py]
        DEF[deferred.py]
        SCH[scheduler.py]
        KEY[keys.py]
    end
    subgraph CORE["Core modules (mirror upstream)"]
        CTX[context.py]
        FIB[fiber.py]
        REF[reflect.py]
        REG[registry.py]
        EVT[events.py]
        SVC[service.py]
        TRC[traceable.py]
        LOG[logger.py]
    end
    PUB["__init__.py — public exports"]
    RT --> CORE
    CORE --> PUB
```

### Async model without an event loop

The Promise-based upstream lifecycle runs on a scheduler:

- A **task** is a native coroutine that a pycordis scheduler drives on the owner thread with
  `send`/`throw`. asyncio is not used.
- A **`Deferred[T]`** is the only awaitable that a task can await, apart from a `Fiber`. Any
  thread can complete it. A task that awaits an incomplete `Deferred` parks. When the `Deferred`
  completes, the task goes back on the ready queue through the scheduler's thread-safe submit
  path.
- An upstream microtask hop (`await Promise.resolve()`) is one scheduler step. Upstream order is
  kept inside a task: a `ctx.use()` call returns before the new fiber's `apply` runs.
- `ImmediateScheduler` drains the ready queue when the outermost pycordis call returns, so a
  top-level `ctx.use()` returns at quiescence. A `Deferred` that completes during a drain queues
  its task in that drain and does not start a nested drain. Tests and headless hosts use it.
  `FramePumpScheduler` runs at most a fixed number of steps per `pump()` call and never blocks. A
  host with a frame loop pumps it once per frame.
- The scheduler holds every parked task strongly until the task resumes. Python runs the `finally` blocks of a suspended coroutine when it is
  collected, so a task that only a `Deferred` callback holds could run teardown at collection.

### Fiber lifecycle

The states and transitions are those of upstream `FiberState`. The epoch digest decides every
transition, as in upstream `Fiber._refresh` / `_setEpoch` / `_reload` / `_unload` /
`_getState`. A raising `apply` sets the epoch to `INACTIVE`, so the fiber unloads its collected
effects before it reaches `FAILED`:

```mermaid
stateDiagram-v2
    [*] --> PENDING: ctx.use()
    PENDING --> LOADING: all injected keys resolve (epoch set)
    LOADING --> ACTIVE: apply finished, epoch unchanged
    LOADING --> UNLOADING: epoch changed during load, or apply raised
    ACTIVE --> UNLOADING: epoch changed or dispose
    UNLOADING --> PENDING: inverses ran, epoch inactive
    UNLOADING --> LOADING: inverses ran, new epoch
    UNLOADING --> FAILED: inverses ran, error recorded
    FAILED --> LOADING: update() / replace()
    PENDING --> DISPOSED: dispose
    FAILED --> DISPOSED: dispose
    UNLOADING --> DISPOSED: dispose finished
```

The root fiber (`uid` 0) is always `ACTIVE`. Its `dispose` is `restart`: it reverts every root
effect and leaves the root in use, as the upstream `Fiber` constructor's root branch does.

## File Structure

The repository scaffold (`pyproject.toml`, `LICENSE`, `README.md`, docs site,
`tests/conftest.py`) exists. This spec adds:

```
src/pycordis/
├── __init__.py           # MODIFY: public exports
├── context.py            # NEW: Context, extend / isolate / intercept, item access
├── fiber.py              # NEW: Fiber, FiberState, EffectHandle, effect(), lifecycle
├── reflect.py            # NEW: ReflectService, Impl, locate(), accessor / mixin
├── registry.py           # NEW: RegistryService, Component forms, Inject, @inject, replace()
├── service.py            # NEW: Service base: provide on construct, hooks, intercept merge
├── events.py             # NEW: EventsService, internal/* events
├── logger.py             # NEW: LoggerService over stdlib logging
├── traceable.py          # NEW: tracker, def-site / use-site views
├── keys.py               # NEW: Key[T] typed handle
├── scheduler.py          # NEW: Scheduler protocol, task trampoline, Immediate / FramePump
├── deferred.py           # NEW: Deferred[T], from_future()
├── disposables.py        # NEW: DisposableList
└── errors.py             # NEW: CordisError, ValidationError, call-site notes
tests/
├── conftest.py           # MODIFY: ported upstream helpers, FakeClock, run fixture
├── test_<upstream>.py    # NEW: one per packages/core/tests/*.spec.ts (17 files)
├── test_effect_forms.py  # NEW: Python-only effect forms
├── test_scheduler.py     # NEW: drain / pump / submit / park-resume
├── test_threading.py     # NEW: owner-thread assertion, cross-thread Deferred completion
└── test_invariants.py    # NEW: randomized-schedule calculus invariants
docs/
├── user_guide.md         # NEW: concepts and host integration
└── architecture.md       # MODIFY: module map, divergence rows
README.md                 # MODIFY: first release, usage pointer
zensical.toml             # MODIFY: user guide nav entry
pyproject.toml            # MODIFY: version 0.1.0
conformance/ledger.toml   # MODIFY: entries for each ported upstream item
```

## Implementation References

- **Upstream source of truth**: `cordiverse/cordis` `packages/core/src/*.ts` and
  `packages/core/tests/*.spec.ts` at revision `68c9d9917871d8680bc7ccb9034b9284d5102433` define
  the behavior to conform to. The paper (arXiv `2608.25512`) defines the contract; for example,
  the dependents guard in `Fiber._unload` keeps `LIFE-ORDER` (Theorem 70).
- **Lifecycle core**: `Fiber._refresh`, `_setEpoch`, `_reload`, `_unload`, `_checkImpl`,
  `_replace`, `_updateState`, `_getState`, `update`, `restart`, `await` in `fiber.ts`, and the
  root branch of the `Fiber` constructor. Port each as an `async def` task that keeps the
  upstream structure (`mirror = true`): `LIFE-ORDER` and `LIFE-COHERENT` depend on it.
- **Effect execution**: `Fiber._execute` and `Fiber.effect` in `fiber.ts`, covering each return
  form, the `epoch` guard on async iteration, the dispose-once wrapper, the `then` of the
  returned `AsyncDisposable` (the port is `EffectHandle.__await__`), and child effect metadata.
- **Provision and release**: `ReflectService.provide`, `notify`, `get`, `_getImpl`, the
  `handler.get` trap, and `locate` in `reflect.ts`. A provider's disposal waits on
  `impl.release` until its dependents leave.
- **Context**: the `Context` constructor (core services, root fiber), `extend`, `isolate`,
  `intercept`, `Context.is`, and the `Context.filter` hook that `EventsService` reads from an
  event's `this` argument, in `context.ts` and `events.ts`.
- **Registry**: `RegistryService.use`, `_forward`, `_create`, `replace`, and `Inject.resolve` in
  `registry.ts`. `replace` moves live fibers to the new component through `Fiber._replace`.
- **Service base**: `Service` constructor, `[symbols.extend]`, `[symbols.mergeIntercept]`, and
  `[symbols.resolveIntercept]` in `service.ts`.
- **Traceable views**: `getTraceable`, `createTraceable`, `createShadow`, `withProps` in
  `utils.ts`. The view is the port of the Proxy. The `spec` handling of `__class__` in
  `unittest.mock` shows how `isinstance` can be true for a forwarding object.
- **Config validation errors**: `ValidationError` (with its static `formatPath`) in `fiber.ts`,
  which upstream raises from `resolveConfig`.
- **Name and test mapping**: [Port an Upstream Change](../developer_guide.md#port-an-upstream-change).

## Key Constraints

### Boundaries

- No runtime dependencies; no imports of `asyncio` or of any host application or GUI toolkit.
  Config validation is duck-typed (see below), so pydantic is not imported.
- Python `>=3.12`. PEP 695 generic syntax is allowed.
- MIT license; `LICENSE` keeps the upstream copyright line.

### Context and access

- `Key[T]` is a typed handle with a `name: str`. Every API that takes a key also takes a plain
  `str` (the upstream names).
- `ctx[key]` is checked access and mirrors the upstream Proxy `get` trap, including its
  `internal/get` waterfall. It raises `CordisError` with the upstream messages:
  `cannot get property "<name>" without inject` and
  `cannot get required service "<name>" in inactive context`. At a context whose def-site fiber
  has no runtime (the root), access is unchecked, as upstream: `ctx[key]` returns the provider's
  value whether or not the provider is `ACTIVE`, or `None` when there is no provider. The static
  type of `ctx[key]` is `T` for a `Key[T]` and `Any` for a `str`; the docstring states the root
  exception.
- `ctx.get(self, key: Keyable[T], strict: bool = True) -> T | None` mirrors
  `ReflectService.get`: it returns `None` when there is no provider, and with `strict=True` also
  when the provider's fiber is not `ACTIVE`.
- Attribute-style service access (`ctx.foo`) is not ported. `Context` members are explicit,
  statically typed methods that forward to the core services (`get`, `set`, `provide`,
  `accessor`, `mixin`, `effect`, `inject`, `use`, `on`, `once`, `emit`, `parallel`, `serial`,
  `bail`, `waterfall`), and read-only typed attributes for the instance attributes that the
  upstream `Context` constructor sets (`root`, `fiber`, `reflect`, `registry`, `events`,
  `logger`). Names that `accessor` / `mixin` register resolve only through `ctx[name]`.
- Derived-context signatures: `extend(self, **meta: object) -> Self`,
  `isolate(self, name: str, label: object | None = None) -> Self`,
  `intercept(self, name: str, config: object) -> Self`. `Context.is(value)` is
  `isinstance(value, Context)`.
- Key access accepts `Key[T]` or `str` throughout: `Keyable[T] = Key[T] | str`. `Key[T]` has
  `__init__(self, name: str)` and a read-only `name`. A `Key`'s equality and `hash` follow its
  name (`Key("a") == "a"`, `hash(Key("a")) == hash("a")`), so both spellings resolve to the same
  `Impl`. Public signatures:
  - `Context.effect(self, fn: Callable[[], EffectForm], label: str = "anonymous") ->
    EffectHandle` — ports the upstream `Fiber.effect` overloads to one entry point.
    `EffectHandle` is a `Disposer` (calling it runs the effect's inverses, each at most once)
    with `__await__`: awaiting it resolves to a disposer when the effect's setup finishes
    (at once for a sync form), as the upstream `AsyncDisposable.then` does.
  - `Context.use(self, component: Component, config: object = None) -> Fiber` — inside a task,
    the call returns before `apply` runs. `fiber.wait() -> Deferred[Fiber]` ports
    `Fiber.await`: it resolves with the fiber when no load or unload is in progress, and rejects
    with the recorded error when the fiber is `FAILED`. A fiber that waits in `PENDING` for a
    provider resolves at once. `Fiber.__await__` forwards to `wait()`.
  - `Fiber.update(self, config: object, no_save: bool = False) -> Deferred[None] | None` —
    `None` when an `internal/update` listener vetoes the restart; an invalid config raises
    `ValidationError` from the call. `Fiber.restart(self) -> Deferred[None]`.
    `Fiber.dispose(self) -> Deferred[None]`.
  - `RegistryService.replace(self, old: Component, new: Component) -> list[Fiber]`.
  - `Context.set(self, key: Keyable[T], value: T) -> None`.
  - `Context.provide(self, key: Keyable[T], value: T) -> Disposer` — ports
    `ReflectService.provide`, whose upstream disposer is the `() => void`.
- Derived contexts (`extend`, `isolate`, `intercept`) are new `Context` objects that point to
  their parent. Isolation and intercept tables are chained mappings (`collections.ChainMap` or
  equivalent). A key is read from the nearest table that owns it, as the upstream
  `Object.hasOwn` + prototype walks do.
- Upstream symbol-keyed hooks become `cordis_`-prefixed attributes: `cordis_init`,
  `cordis_check`, `cordis_invoke`, `cordis_extend`, `cordis_merge_intercept`,
  `cordis_resolve_intercept`, `cordis_metadata`, `cordis_tracker`, `cordis_filter`. Do not add
  dunder names. Internal symbols (`receiver`, `original`, `decorator`, `initHooks`,
  `checkProto`, `effect`) become private attributes.
- The tables that upstream tests read through `ctx[Context.isolate]` and
  `ctx[Context.intercept]` are readable through public read-only mappings on `Context`, like the
  shadow and caller introspection below.

### Effects

- `ctx.effect(fn, label="anonymous")` accepts a callable that returns one of: `None`, a disposer
  callable, a sync iterator of disposers, an async iterator of disposers, or a coroutine that
  returns a disposer — the upstream forms. Any other return raises `TypeError("Invalid effect")`.
  Every form returns an `EffectHandle`.
- `Disposer = Callable[[], Deferred[None] | None]`. Returning a `Deferred` is the port of the
  upstream `Promise`-valued dispose; a disposer that does not wait returns `None`.
- The inverses that one effect collects run newest first, and an inverse that returns a
  `Deferred` makes the next one wait for it (the chain in upstream `Fiber.effect`'s `dispose`).
  Separate effects of one fiber dispose concurrently (see `_unload` below).
- A disposer runs at most once. A second call does nothing (upstream `runner.epoch` guard). An
  async iterator stops at the first iteration boundary after its effect is disposed, and its
  `aclose()` runs so that `finally` blocks execute.
- An effect belongs to the fiber that created it, not to the returned handle: the fiber holds
  the effect's resources, so they live exactly as long as the fiber, and no internal link to
  them is a weak reference. Dropping a handle can therefore neither close resources early nor
  trigger teardown at collection. Disposal is explicit: pycordis adds no finalizer (the upstream
  core has none) and its types do not implement `__del__`.
- Root disposal reverts every root effect (it is a root restart, as upstream). An effect that
  an inverse registers on the root during that teardown stays registered, as upstream; pycordis
  also logs it with its label at level `DEBUG`, so the host sees an effect that root disposal
  did not revert (divergence `root-late-effect`).
- A coroutine or async generator can await only a `Deferred` or an object whose `__await__`
  forwards to one (`Fiber`, `EffectHandle`). Awaiting any other object raises `TypeError` at
  the `await`.
  `Deferred.from_future(concurrent.futures.Future)` connects work that runs on other threads.
- `Fiber._unload` starts disposers in reverse registration order. Synchronous disposers run one
  after another. Disposers that return a `Deferred` complete concurrently, as upstream
  `Promise.all` does. A disposer exception is logged with the effect label and does not stop the
  others.
- Error context: `ctx.use()` and `ctx.effect()` capture their call site at low cost (frame
  objects, not formatted strings). On an exception, the formatted call site is attached with
  `BaseException.add_note` (the port of `composeError` / `buildOuterStack`).

### Lifecycle and services

- State names, the epoch digest (`"<revision>:<impl uid>…"`), the `INACTIVE` sentinel, and the
  transitions match `fiber.ts`. Provider identity is the `Impl.uid`, never value equality. A
  provider that changes its value in place with `ctx.set` is not observed (upstream behavior).
- A duplicate `provide` in one realm raises `service "<name>" has been registered at <fiber>`.
- A fiber unloads only after every dependent of its provisions has unloaded (`impl.dependents` /
  `_release`). A provision's disposer waits on `impl.release` before it removes itself.
- An exception from `apply` is logged. The fiber's collected effects are disposed, and the fiber
  goes to `FAILED`. It loads again only through `update()` — which clears the recorded error and
  restarts through the default path of the `internal/update` waterfall — or through
  `RegistryService.replace()`. A direct `restart()` on a failed fiber re-raises the recorded
  error instead of recovering (upstream `_setEpoch` returns early while an error is stored).
- Component forms: a function `(ctx, config)`, a class whose `__init__(self, ctx, config)` is the
  constructor form (`inspect.isclass`), or an object with `apply(ctx, config)`. Metadata
  attributes: `name`, `Config`, `inject` (a list of names or keys, or a dict from name to
  intercept config), `provide` (a `str` or a list), `intercept`. Components set these attributes
  directly on the function, class, or object, as upstream does; pycordis adds no metadata
  decorator. `@inject(name, config)` ports the upstream class and method decorator, including
  method-level init hooks. On a subclass, the decorator copies the inherited `inject` mapping
  before it adds a name (upstream `checkProto`); it never changes the base class's mapping. The
  insertion order of `inject` is the order of the epoch digest. `Component` is a public type
  alias for the three forms.
- `Config` is either an object with a `model_validate` callable (pydantic-compatible) or a
  callable transform `(raw) -> validated` (upstream `Component.Transform`). A failure becomes a
  pycordis `ValidationError` whose message uses the upstream `formatPath` layout.
  `ValidationError` subclasses `TypeError`, as upstream `ValidationError extends TypeError` does.
  Issue paths are read from the validator error's `errors()` method when it has one.
- `EventsService` keeps the upstream event names (`internal/plugin`, `internal/status`,
  `internal/service`, `internal/update`, `internal/config`, `internal/get`, `internal/set`,
  `internal/listener`, `internal/dispatch`, `internal/commit`, `internal/replace`) and dispatch
  modes. `parallel` and `serial` return a `Deferred`. `parallel` reports listener failures as one
  `ExceptionGroup` (the port of `AggregateError`). A dispatch with no listener allocates
  nothing, because `ctx[key]` runs the `internal/get` waterfall on each access.
- The fiber logger is `logging.getLogger("pycordis." + fiber.name)`. The fiber logs `apply` and
  disposer errors to it; `LoggerService` exposes it. The library adds no handlers.
- Traceable views: a service opts in with `cordis_tracker` (`property`, `associate`,
  `no_shadow`). Access through a context returns a view:
  - The tracked property (normally `ctx`) reads as the use-site context.
  - Methods run with the use-site context, so an effect that a service method creates when it is
    called through a view belongs to the caller's fiber.
  - `isinstance(view, ServiceClass)` is true.
  - The def site decides resolution. The use site decides isolate, intercept, and effects.
  - The shadow chain and the caller context are readable through a public introspection
    attribute (one on the view, one on the context), so the upstream tests that walk
    `ctx[symbols.shadow]` and read `symbols.caller` port without private access.
  - Only these semantics are required, not a specific mechanism. `test_associate.py`,
    `test_shadow.py`, and `test_invoke.py` (translated from upstream) check them.
- Deprecated upstream aliases (`ctx.plugin`, `EventsService.dispatch`) are not ported.

### Scheduling and threads

- Every `Context`, `Fiber`, and core-service method asserts that it runs on the owner thread. A
  call from another thread raises `CordisError("WRONG_THREAD")`. Only two operations are
  permitted from other threads: `Scheduler.submit(fn)` and `Deferred` completion.
- `ImmediateScheduler.run_until_quiet(timeout)` also blocks on parked `Deferred`s, so a headless
  host can finish deferred teardown before it exits. `FramePumpScheduler.pump(max_steps)` never
  blocks.
- An empty `pump()` returns in constant time and allocates nothing, because a frame-loop host
  calls it on every frame.
- Signatures: `Scheduler.submit(self, fn: Callable[[], None]) -> None`;
  `FramePumpScheduler.pump(self, max_steps: int) -> int` (the number of steps it ran);
  `ImmediateScheduler.run_until_quiet(self, timeout: float | None = None) -> None`, raising
  `TimeoutError` past `timeout`. `Deferred[T]`: `done() -> bool`, `result() -> T` (raises the
  rejection error, or `CordisError("DEFERRED_PENDING")` while incomplete; it never blocks),
  `resolve(self, value: T) -> None`, `reject(self, error: BaseException) -> None`,
  `__await__() -> Generator[Any, None, T]`,
  `from_future(cls, future: concurrent.futures.Future[T]) -> Deferred[T]`.

### Host contract

These are the public guarantees that a host application builds on. Each is part of the public API
and is tested through it.

- **Scheduler selection**: the host creates the root `Context` with a scheduler. A frame-loop host
  can drain a `FramePumpScheduler` to quiescence once (for example, before its first frame) and
  then pump it per frame.
- **Thread-safe submit**: `Scheduler.submit(fn)` is the one entry point for work that starts on
  other threads (file watchers, worker pools). `fn` runs on the owner thread at the next step.
- **Registry adapters**: a host registry that wants revertible entries exposes
  `add(entry) -> Disposer` and is called through `ctx.effect` on the caller's fiber. The user
  guide documents this pattern; the library provides no host registry.
- **Read-only introspection**: `fiber.name`, `fiber.state`, `fiber.uid`, `fiber.inject`,
  `fiber.committed`, `fiber.provisions`, `fiber.parent`, `fiber.children`,
  `fiber.get_effects()` (a tree of effect labels), `fiber.error` (the last `apply` exception, or
  `None`), and the `internal/status` event. A host builds status views and component APIs from
  these without private access. pycordis stores no host data on a fiber; a host that associates
  data with fibers keys its own table by the fiber.

## Integration

- `pycordis/__init__.py` exports the public API: `__version__` (present), `Context`, `Fiber`,
  `FiberState`, `Key`, `Service`, `Deferred`, `Scheduler`, `ImmediateScheduler`,
  `FramePumpScheduler`, `CordisError`, `ValidationError`, `EffectHandle`, `inject`, and the
  `Disposer`, `Keyable`, and `Component` type aliases (public signatures use them).
- `docs/user_guide.md` (new nav entry in `zensical.toml`): concepts (context, fiber, effect,
  service, key), choosing a scheduler, the host contract, and the coeffect rule: bind a service
  at a key for the lifetime of its provider, and keep a value that changes often inside a stable
  service that publishes change events, because a rebind reloads every dependent.
- `docs/architecture.md`: the module map and one divergence row for each divergence in
  [Appendix A](#appendix-a-divergences), added in the increment that implements it.
- `conformance/ledger.toml`: each increment changes the `pending` entries of the upstream items
  that it implements to their reviewed status, with the target, the divergence ID, the hash,
  and `mirror = true` for the fiber state machine and the epoch digest.
- Release: zero `pending` ledger entries and an empty `[contract] pending`; version `0.1.0` in
  `pyproject.toml`; tag `v0.1.0`.

## Testing

- ⬜ Each of the 17 upstream `packages/core/tests/*.spec.ts` files (associate, component,
  decorator, dispose, events, fiber, intercept, invoke, isolate, logger, reflect, replace,
  service-dispose, service, shadow, update, utils) has a `tests/test_<name>.py` whose tests carry
  the `upstream` markers of the cases they translate. An upstream case that pycordis does not
  follow has a `diverged` ledger entry and a test marked with its divergence. A module is
  complete when it holds every translatable upstream case: `test_utils.py` by Increment 1,
  `test_events.py` by Increment 3, `test_dispose.py` and `test_reflect.py` by Increment 5,
  `test_component.py`, `test_service.py`, and `test_intercept.py` by Increment 6,
  `test_fiber.py` and `test_isolate.py` by Increment 7, `test_update.py`, `test_replace.py`, and
  `test_service_dispose.py` by Increment 8, `test_associate.py`, `test_shadow.py`, and
  `test_invoke.py` by Increment 9, and `test_logger.py` and `test_decorator.py` by Increment 10.
  A module shared before its upstream dependencies exist holds the subset that runs and grows in
  the increment that unblocks the rest.
- ⬜ The upstream test helpers (`tests/utils.ts`: `withTimers`, `sleep`, `event`, `Counter`,
  `getHookSnapshot`, `Filter`, `Session`; and the `Events` entries of `events.types.ts`) port
  into fixtures and small classes in `tests/conftest.py`, not into a separate helpers module.
  A `FakeClock` fixture replaces `withTimers`: `sleep(ms) -> Deferred[None]`, and `advance(ms)`
  completes each due `Deferred` in deadline order and drains the scheduler after each one (the
  port of `vi.advanceTimersByTimeAsync` / `vi.runAllTimersAsync`). A `run` fixture drives an
  async upstream test body as a task to quiescence. `vi.fn` / `mock.fn` become small recording
  fakes. No wall-clock waiting.
- ⬜ Effect forms: a disposer, a sync iterator, an async generator that awaits a `Deferred`, and
  a coroutine each register and dispose in LIFO order, and awaiting each form's `EffectHandle`
  resolves when its setup finishes. An invalid return (including a context manager) raises
  `TypeError`. Awaiting an object that is not a `Deferred`, `Fiber`, or `EffectHandle` raises
  `TypeError` (parametrized).
- ⬜ A disposer called twice runs once. An exception in one disposer is logged, and the other
  disposers still run. Dropping the handle returned by `ctx.effect()` does not dispose the
  effect.
- ⬜ Root disposal reverts every root effect (`contract("EFF-REVERT")`). An effect that an
  inverse registers on the root during that teardown stays registered and is logged at level
  `DEBUG` (`divergence("root-late-effect")`).
- ⬜ Inside a task, `ctx.use()` returns before the child's `apply` runs. `ImmediateScheduler`
  reaches quiescence when the outermost call returns. `fiber.wait()` resolves with the fiber
  while it waits in `PENDING` for a provider.
- ⬜ `FramePumpScheduler.pump(n)` runs at most `n` steps. `pump()` on an empty queue returns `0`
  in constant time. A `tracemalloc` check supports the no-allocation intent: after a warm-up
  call, a window containing only `pump()` reports no new traces. If the interpreter adds noise
  on a platform, that check warns and the timing assertion still gates.
- ⬜ A call from a second thread raises `WRONG_THREAD` (`divergence("owner-thread")`). A
  `Deferred` completed on a worker thread resumes its parked task at the next owner-thread step.
  `submit` from a worker thread runs its function on the owner thread.
- ⬜ A provider stays `UNLOADING`, with its effects in place, until the teardown of its dependent
  finishes reading the service. Then the provider's inverses run (`contract("LIFE-ORDER")`).
- ⬜ A dependent in `LOADING` whose provider is replaced exits through `UNLOADING` and reloads
  against the new provider. No iteration runs against two resolutions
  (`contract("LIFE-COHERENT")`).
- ⬜ An `apply` that raises gives `FAILED` through `UNLOADING`, with `fiber.error` set, every
  effect collected before the raise disposed, and no state change in the parent or siblings.
  `update()` recovers the fiber (`contract("LIFE-FAILURE")`, `divergence("host-introspection")`).
- ⬜ `RegistryService.replace(old, new)` moves live fibers to the new component and reloads them;
  the inverses of the old component run first.
- ⬜ Introspection: `get_effects()` returns the label tree in registration order, and
  `fiber.children` covers the live fibers of each runtime across reloads
  (`divergence("host-introspection")`).
- ⬜ Invariant suite (`test_invariants.py`, seeded `random.Random`, parametrized over seeds):
  random interleavings of `use` / `dispose` / provider replacement over a small component pool
  whose `apply` functions do not raise satisfy all of these, each test marked with its contract
  ID:
  - (a) `EFF-REVERT`: disposing a fiber restores every provider table and event-hook table to
    the state without it;
  - (b) `LIFE-ORDER`: a provider never runs inverses while a dependent is installed;
  - (c) `LIFE-PROGRESS`: every schedule reaches quiescence;
  - (d) `LIFE-CONFLUENT`: at quiescence, the context's observable structure matches that of a static load of the
    same components in pool listing order (the test fixture lists the pool, the reference load
    uses that order): the same fiber tree, the same fiber for each key from `ctx.get`, and the
    same effect labels per fiber in registration order;
  - (e) `EFF-ISOLATE`: components that provide and read different keys reach the same state
    when their steps interleave as when they run one after the other;
  - (f) `REG-WELLFORMED`: after every step, a key has at most one provider in a realm, and each
    `fiber.parent` names a live fiber.

  Mark the module `slow` if a seed measures ≥ 0.1 s.
- ⬜ The error note on a failed `apply` names the `ctx.use()` call site.

## Dependencies

- **Prerequisite**: the [Upstream Conformance](completed/conformance.md) spec is complete before
  Increment 1. It registers the `upstream`, `contract`, and `divergence` markers (`pyproject.toml`
  sets `--strict-markers`, so a marked test fails collection without them), and it supplies
  `conformance/upstream.json` and the `pending` ledger stubs that each increment reviews.
- **Runtime**: none.
- **Dev**: `pytest`, `ruff` (present). The `model_validate` config tests use a stdlib fake
  validator (a class whose `model_validate` raises an exception carrying an `errors()` method),
  not a validator dependency.
- **Docs**: `zensical`, `mkdocstrings[python]`, `pymdown-extensions` (present).

## Migration Strategy

Validate each increment with the command that it names, then `uv run ruff check .` and
`uv run ruff format --check .`. Each increment adds the divergence rows and docstrings for what
it implements.

### Increment 1: Disposables, Deferred, ImmediateScheduler
**Goal**: `DisposableList`, `Deferred`, the task trampoline, and `ImmediateScheduler` work alone.
**Files**: `disposables.py` (~70), `deferred.py` (~90), `scheduler.py` (~150), `errors.py`
(~60), `tests/conftest.py` (`FakeClock`, `run`), `test_utils.py`, `test_scheduler.py`
**Validation**: `uv run pytest tests/test_utils.py tests/test_scheduler.py`
**Status**: ✅ Completed

**Changed**:
- `src/pycordis/errors.py`: `CordisError`, and `CallSite` (`capture`, `format`, `annotate`) that adds a call site to an exception as a note
- `src/pycordis/disposables.py`: `DisposableList` with `push`, `unshift`, `delete`, `clear`, `len`, and snapshot iteration
- `src/pycordis/deferred.py`: `Deferred` with `resolve`, `reject`, `result`, `done`, `add_done_callback`, `resolved`, `from_future`, and `__await__` (always one scheduler step)
- `src/pycordis/scheduler.py`: `Scheduler` base class (`submit`, `call`, `hold`, `release`), `ImmediateScheduler` (`run_until_quiet`), the task trampoline, and `spawn`
- `tests/conftest.py`: `FakeClock`, and the `scheduler`, `clock`, and `run` fixtures
- `conformance/ledger.toml`: the 12 `DisposableList` and call-site items and the 6 `utils.spec.ts` cases are reviewed
- `docs/architecture.md`: divergence row `error-notes`

**Tests**:
- `tests/test_utils.py` (11 tests) and `tests/test_scheduler.py` (25 tests): 36 pass
- Re-run for regression: `tests/test_conformance.py`, `tests/test_inventory.py`, `tests/test_docstrings.py` (139 pass in all)
- The task-hold test fails when `Scheduler.hold` is disabled

**Deviations**:
- `ValidationError` is not in `errors.py`. Config validation needs it, so it is added with the registry
- `CallSite` stores `(code, line number)` pairs, not frame objects, so it does not keep local variables alive
- `Scheduler` is an abstract base class, not a `Protocol`: it holds the parked tasks that keep a suspended coroutine alive. `call()` is the span of an outermost call, which `ImmediateScheduler` uses to drain
- `scheduler.py` imports `deferred.py`, because the task trampoline must recognize a `Deferred`. This differs from the rule that runtime modules import only `errors`. `deferred.py` imports only `errors`, so there is no cycle
- Divergence rows `async-model`, `async-effects`, and the other adaptation rows are not added yet: no ledger entry names them, and the divergence check rejects a row that nothing names. Each row is added with the first item that it covers
- `run` returns the task's `Deferred`, so a test can advance the clock before it reads the result
- Owner-thread checks are not added yet (planned for the threading increment)
- `ImmediateScheduler.run_until_quiet` documents the owner-thread rule but does not assert it

**Manual validation**:
- A task that awaits a `Deferred` parks in an `ImmediateScheduler`, and resolving the `Deferred` resumes it (confirmed by the user)

### Increment 2: Context and effects
**Goal**: `Context` with `extend` / `isolate` / `intercept`, the root `Fiber`, the fiber logger,
and `ctx.effect` in every return form, with `EffectHandle`, LIFO dispose, dispose-once, root
restart, and call-site notes.
**Files**: `context.py` (~120), `fiber.py` (effect part, ~220), `keys.py` (~30),
`test_effect_forms.py`, `test_dispose.py`
**Validation**: `uv run pytest tests/test_effect_forms.py tests/test_dispose.py`
**Status**: ✅ Completed

**Changed**:
- `src/pycordis/keys.py`: `Key[T]` (equality and hash follow the name) and the `Keyable[T]` alias
- `src/pycordis/context.py`: `Context(scheduler)` with `root`, `fiber`, `scheduler`, `extend`, `isolate`, `intercept`, `effect`, and the read-only mappings `meta`, `isolates`, `intercepts`
- `src/pycordis/fiber.py`: `Fiber` (root only), `FiberState`, `EffectHandle`, `EffectMeta`, `Disposer`, `EffectForm`, `INACTIVE`; `effect()` in every return form with the call-site note, the `root-late-effect` log, and `get_effects()`
- `src/pycordis/fiber.py`: the root lifecycle (`_get_state`, `_update_state`, `_refresh`, `_set_epoch`, `_reload`, `_unload`), `wait()`, `restart()`, and `dispose()` (the root restart)
- `src/pycordis/scheduler.py`: `start`, which runs the first step of a task at once
- `src/pycordis/disposables.py`: the remover that `push` and `unshift` return gives `None`, as its signature states
- `tests/conftest.py`: the `root` fixture
- `conformance/ledger.toml`: reviewed the `Context` items, the effect and state items of `fiber.ts`, the 13 runnable `dispose.spec.ts` cases, the two effect messages, and the six states; `EFF-REVERT` leaves `[contract] pending`
- `docs/architecture.md`: divergence rows `async-effects`, `fiber-wait`, `hook-names`, `root-late-effect`

**Tests**:
- `tests/test_dispose.py` (13 upstream cases in 9 tests), `tests/test_effect_forms.py` (16 tests, one parametrized over five effect forms, with the `EFF-REVERT` contract test and the `root-late-effect` divergence test), `tests/test_context.py` (7), `tests/test_keys.py` (2)
- Full suite: 187 pass, which includes `tests/test_conformance.py`, `tests/test_inventory.py`, `tests/test_docstrings.py`, `tests/test_utils.py`, and `tests/test_scheduler.py`

**Deviations**:
- The lifecycle methods that the root restart needs are ported now, without `internal/*` events, provisions, `inject`, and the dependents guard. The ledger items `_getState`, `_updateState`, `_setEpoch`, `_refresh`, `_reload`, and `_unload` stay `pending` until they are complete
- `dispose.spec.ts` cases "dispose by component" (needs `ctx.use`) and "yield dispose" (needs `ctx.on`) stay `pending`
- `tests/test_context.py` and `tests/test_keys.py` are not in the file list; they test the public derivation API and `Key`
- The spec does not name the read-only tables. They are `Context.meta` (the values of `extend`), `Context.isolates`, and `Context.intercepts`
- `Fiber.dispose` is a method that calls `restart`; the non-root form replaces it with the unload of the fiber's effect
- The test for awaiting an object that is not a `Deferred`, `Fiber`, or `EffectHandle` is not added again: `tests/test_scheduler.py` owns that rule
- `DisposableList.push` and `unshift` returned a remover that gave a `bool`; the effect code treated that value as a pending `Deferred`. The remover now gives `None`
- Errors that a disposer raises during teardown are logged with the effect label but carry no call-site note
- Owner-thread checks are not added yet (planned for the threading increment)

**Manual validation**:
- A root context registers effects, reverts them newest first with `handle()`, and `fiber.dispose()` leaves the root `ACTIVE` (confirmed by the user)

### Increment 3: Events
**Goal**: `EventsService` with every dispatch mode, listener effects, `cordis_filter`, and the
`internal/*` event names, so that the core services can emit and run waterfalls.
**Files**: `events.py` (~200), `test_events.py`
**Validation**: `uv run pytest tests/test_events.py`
**Docs**: `docs/architecture.md` divergence rows; docstrings of `events.py` and the `Context` dispatch methods
**Status**: ✅ Completed

**Changed**:
- `src/pycordis/events.py`: `EventsService` (`emit`, `bail`, `serial`, `parallel`, `waterfall`, `on`, `once`, `listener_counts`), `EventOptions`, `Hook`, `is_bailed`, and the `EventName`, `DispatchMode`, `Listener`, and `InternalEventName` aliases
- `src/pycordis/context.py`: `Context.events`, shared by every derived context, and the methods `on`, `once`, `emit`, `bail`, `serial`, `parallel`, `waterfall`
- `tests/conftest.py`: `event`, `Symbol`, `Recorder`, `Session`, `Filter`, `hook_snapshot`
- `conformance/ledger.toml`: reviewed 29 `events.spec.ts` cases as ported and 2 as diverged, the `events.ts` items that are complete, the `events.types.ts` items, and the two event messages
- `docs/architecture.md`: divergence rows `async-model`, `exception-group`, `event-dispatch-args`, `deprecated-aliases`

**Tests**:
- `tests/test_events.py`: 51 tests that carry the 29 runnable upstream cases, and Python-only tests for bail results, async `serial`, prepend, global listeners, `internal/dispatch`, and `internal/listener`
- Full suite: 238 pass, which includes `tests/test_conformance.py`, `tests/test_inventory.py`, and `tests/test_docstrings.py`

**Deviations**:
- `this` is the keyword `this=` and an event name is any hashable object (user-approved). Listeners never receive `this`. This is the divergence `event-dispatch-args`
- The listeners of `internal/update` (the fiber listener hook and the default waterfall step) and of `internal/config` are not registered yet: nothing dispatches those events until `Fiber.update` and config resolution exist (Increment 8 and 5). The ledger items `EventsService`, `EventsService.constructor`, `EventsService.on`, and `EventsService.once` stay `pending`
- `EventsService.on` does not wrap the listener with the traceable view (`reflect.bind`): it needs `traceable.py` (Increment 9)
- The events cases `skips a listener disposed by an earlier component during dispatch` and the five `internal/update` cases need `ctx.use` and `Fiber.update`, so they stay `pending`. The `tests/events.spec.ts` helper items stay `pending` for the same reason
- `EventsService.listener_counts()` is added for tests and host status views, because the hook table is private. It is not in the plan
- Only the `internal/dispatch` event item is reviewed; the other ten `event:internal/*` items stay `pending` until their events are emitted
- `Context.on` and the other methods take `EventOptions | bool`, where the `bool` is `prepend`; `EventOptions.is_global` replaces `global`, which is a Python keyword

**Manual validation**:
- A root context registers a listener with `ctx.on`, dispatches with `emit`, runs a `waterfall` chain, and `listener_counts()` shows the removal (confirmed by the user)

### Increment 4: Reflect — provide, get, set, notify, isolation
**Goal**: `ReflectService` with `Impl`, `locate`, checked and lenient access (with the
`internal/get` / `internal/set` waterfalls and unchecked root access), `accessor` / `mixin`, and
realm-scoped provision with duplicate detection.
**Files**: `reflect.py` (~260), `test_reflect.py` (the cases that need no component loading)
**Validation**: `uv run pytest tests/test_reflect.py`
**Docs**: `docs/architecture.md` divergence row; docstrings of `reflect.py` and the `Context` access methods
**Status**: ✅ Completed

**Changed**:
- `src/pycordis/reflect.py`: `ReflectService` (`read`, `write`, `get`, `set`, `provide`, `notify`, `accessor`, `mixin`), `Impl`, and `Accessor`
- `src/pycordis/context.py`: `Context.reflect`, `get`, `set`, `provide`, `accessor`, `mixin`, `declare_realm`, and item access (`ctx[key]`, `ctx[key] = value`)
- `src/pycordis/fiber.py`: `Fiber.provisions`
- `src/pycordis/keys.py`: `key_name`, which turns a `Key` or `str` into the service name
- `conformance/ledger.toml`: reviewed `Context.is()`, `Property`, `ReflectService.accessor`, and five items of `reflect.ts` that a Python context does not need
- `docs/architecture.md`: divergence row `service-access`

**Tests**:
- `tests/test_reflect.py`: 47 pass (one upstream case, the `EFF-REVERT` contract test over 20 seeds, and Python-only tests)
- Full suite: 285 pass, which includes `tests/test_conformance.py`, `tests/test_inventory.py`, and `tests/test_docstrings.py`

**Deviations**:
- Checked access is not added: `locate`, the two access error messages, and the `internal/get` and `internal/set` waterfalls need a fiber with a runtime (user-approved, planned for Increment 5). `ctx[key]` is unchecked
- `ReflectService.set` does not check `cannot set property "<name>" in multiple fibers`, and `notify` does not refresh fibers: no non-root fiber exists to test them (Increment 5)
- `Impl.check` and the `check` argument of `provide` are not added: nothing reads them before the `Service` base (Increment 6)
- `Context.__setitem__` is added, because an accessor setter and a service write have no other entry point
- `Context.declare_realm` is added: the default realm of a service name lives in the root context, and `ReflectService` cannot write it through the read-only `isolates` mapping
- The core mixins of the upstream constructor (`reflect`, `fiber`, `registry`, `events`) are not registered: `Context` has explicit methods
- Ledger items `ReflectService`, `.constructor`, `.get`, `._getImpl`, `.set`, `.provide`, `.notify`, `.mixin`, `Impl`, and `module ./context.ts` stay `pending` until the deferred parts and the traceable views exist; the other five `reflect.spec.ts` cases need `ctx.use`
- `ReflectService.mixin` accepts only a service key as its source, not an object
- Provider removal uses `Scheduler.start`, so `impl.release` is set before the removal returns

**Manual validation**:
- A root context provides a service, reads it through `get`, `ctx[key]`, and a mixin, isolates a realm, hears `internal/service` on removal, and has no provisions afterward (confirmed by the user)

### Increment 5: Registry and core loading
**Goal**: `ctx.use` / `ctx.inject` in the three component forms with `Config` validation, and the
epoch-driven `_refresh` / `_set_epoch` / `_reload` / `_unload` for a component that loads,
provides, injects, and unloads cleanly — without the dependents guard or the failure paths.
**Files**: `registry.py` (~200), `fiber.py` (lifecycle, ~200), `test_component.py`,
`test_dispose.py`, `test_reflect.py`
**Validation**: `uv run pytest tests/test_component.py tests/test_dispose.py
tests/test_reflect.py`
**Docs**: `docs/architecture.md` divergence rows; docstrings of `registry.py`, `Context.use`, and the new `Fiber` members
**Status**: ✅ Completed

**Changed**:
- `src/pycordis/registry.py`: `Component`, `Applicable`, `Runtime`, `resolve_inject`, and `RegistryService` (`use`, `inject`, `resolve`, `get`, `has`, `keys`, `values`, `entries`, `for_each`, `unregister`, `next_uid`, `len`)
- `src/pycordis/fiber.py`: non-root `Fiber` (uid, child context, `internal/plugin`, `ctx.use()` effect, `dispose`, `name`, `committed`, `inject`, `runtime`), the epoch digest in `_refresh`, `_check_impl`, `internal/status` and provider announcement in `_update_state`, `_apply` (function, class, and `cordis_init`), `_resolve` (the `internal/config` waterfall), and `resolve_config`
- `src/pycordis/errors.py`: `ValidationError` and `Issue`
- `src/pycordis/reflect.py`: `locate`, checked `read` with the `internal/get` waterfall, the `internal/set` waterfall in `write`, the `in multiple fibers` check in `set`, fiber refresh in `notify`, and `find` and `announce`
- `src/pycordis/context.py`: `registry`, `use`, `inject`, `bind_fiber`, `isolated_since`, and the root listener of `internal/config`
- `src/pycordis/events.py`: `EventsService.install`, and a waterfall without listeners that calls its final function at once
- `conformance/ledger.toml`: 59 entries reviewed (18 `component.spec.ts`, `dispose.spec.ts`, and `reflect.spec.ts` cases; the state-machine items `_getState`, `_updateState`, `_setEpoch`, `_refresh` with `mirror = true`; `ValidationError`, `resolveConfig`, `locate`, and the registry items; six events)
- `docs/architecture.md`: divergence row `config-schema`; row `service-access` covers checked access and the `internal/get` and `internal/set` arguments

**Tests**:
- `tests/test_component.py` (43 pass: 10 `component.spec.ts` cases, `dispose by component`, the three forms, names, `inject` spellings and errors, shared runtimes, `internal/plugin` and `internal/status`, config validation, loading by injection, and the call-site note), `tests/test_reflect.py` (58 pass, 5 `reflect.spec.ts` cases and Python-only tests of checked access, listeners, realms, and `set`), `tests/test_dispose.py` (14 pass, includes `yield dispose`)
- Full suite: 340 pass, which includes `tests/test_conformance.py`, `tests/test_inventory.py`, `tests/test_docstrings.py`, `tests/test_events.py`, `tests/test_effect_forms.py`, and `tests/test_scheduler.py`
- Disabling the checked-access error, or the realm check in `locate`, fails a test. A digest that leaves out the provider `uid` fails no test yet: a provider change always passes through `INACTIVE` until `replace` exists (Increment 7 and 8)

**Deviations**:
- `ctx.plugin` and `registry.plugin` are not ported: ledger status `diverged` with `deprecated-aliases`
- The `access error` case does not assert JavaScript stack lines, `access check` does not read `prototype`, and `service injection` does not check logger warnings (the logger is Increment 10). A check of `ctx.meta` replaces the last one. These cases are `adapted` with `service-access`
- `internal/get` and `internal/set` listeners get no `error` argument (row `service-access`)
- `RegistryService.use` and `inject` take the calling context as the first argument, like `ReflectService`
- `Fiber.parent` is the `Context` that created the fiber
- `EventsService.install`, `Context.bind_fiber`, `Context.isolated_since`, `ReflectService.find`, and `ReflectService.announce` are added; the plan does not list them
- The dependents tracking, the wait in `_unload`, `_forward`, and the alias table are not added (user-approved, Increment 8). The ledger items `Fiber`, `_reload`, `_unload`, `_replace`, `update`, `RegistryService.use`, `.get`, `.has`, `.replace`, and `._forward` stay `pending`
- `Fiber._checkImpl` stays `pending` because `Impl.check` needs the `Service` base (Increment 6)
- `dispose.spec.ts` `yield dispose` had no remaining blocker, so it is ported here
- Three tests of `tests/test_events.py` compare listener counts with a baseline snapshot, because a root has the listener of `internal/config`
- Owner-thread checks are not added yet (planned for the threading increment)

### Increment 6: Service base
**Goal**: The `Service` base with provide on construct, the `cordis_*` hooks, and intercept merge
and resolve.
**Files**: `service.py` (~100), `test_service.py`, `test_intercept.py`, `test_component.py`
**Validation**: `uv run pytest tests/test_service.py tests/test_intercept.py
tests/test_component.py`
**Docs**: `docs/architecture.md` divergence row; docstrings of `service.py`
**Status**: ✅ Completed

**Changed**:
- `src/pycordis/service.py`: `Service` (constructor that provides `self` under its name or its `provide` attribute, `cordis_filter`, `cordis_merge_intercept`, `cordis_resolve_intercept(caller=None)`); an optional `cordis_check` and `cordis_init` are read from the subclass
- `src/pycordis/reflect.py`: `Impl.check`, the `check` argument of `ReflectService.provide`, and `Located.fiber`
- `src/pycordis/fiber.py`: `Fiber._check_impl` calls `Impl.check`; a false result or an exception hides the provider, and an exception is logged
- `src/pycordis/context.py`: the `check` argument of `Context.provide`, and `Context.intercept_layers`
- `conformance/ledger.toml`: reviewed `Fiber._checkImpl`, `Service.[symbols.filter]`, `.[symbols.mergeIntercept]`, `.[symbols.resolveIntercept]`, 3 `service.spec.ts` cases, 2 `hasInstance` cases, and 6 `intercept.spec.ts` cases
- `docs/architecture.md`: divergence row `intercept-caller`

**Tests**:
- `tests/test_service.py` (16 pass: 5 upstream cases, and Python-only tests of the name, the `check` hook with its log, and `cordis_filter`), `tests/test_intercept.py` (7 pass: 6 upstream cases and the default caller)
- Full suite: 363 pass, which includes `tests/test_conformance.py`, `tests/test_inventory.py`, and `tests/test_docstrings.py`
- A reversed order of intercept layers, a `check` result that is ignored, and an `inject` configuration for the fiber's own provision each fail a test

**Deviations**:
- `cordis_resolve_intercept` takes an explicit `caller` context (user-approved, divergence `intercept-caller`). Increment 9 removes the argument when the traceable view supplies the caller, and removes the divergence row
- `intercept.spec.ts` `take injected config from the def site` stays `pending`: it needs the use-site view of Increment 9. Its test goes in `tests/test_intercept.py` then
- `service.spec.ts` `traceable effect (with inject)` and `(without inject)` wait for Increment 9, and `dispose dependents before provider`, `... restarts`, and `... isolated provider` wait for Increment 8; they stay `pending`
- The tracker, the callable form (`cordis_invoke`), `cordis_extend`, and `Service.[Symbol.hasInstance]` are not added: they belong to the traceable view (Increment 9). Ledger items `Service`, `Service.constructor`, `.[symbols.extend]`, `.[Symbol.hasInstance]`, `Impl`, and `ReflectService.provide` stay `pending`
- `Fiber._check_impl` logs a failed check to the fiber logger of the provider; `LoggerService` is Increment 10
- `Service` is not exported from `pycordis/__init__.py` yet (release increment)
- The `test_component.py` file needed no change: its `Service.init` case was ported in Increment 5

**Manual validation**:
- A root context provides a `Service` subclass, resolves intercept configuration from two `intercept` layers, and resolves the `inject` configuration inside an `inject` callback (confirmed by the user)

### Increment 7: Fiber lifecycle semantics
**Goal**: `FAILED` from a raising `apply` (through `UNLOADING`), reload on a provider change,
required-key absence parking a fiber at `PENDING`, `fiber.wait()`, and `internal/status` per
provider — the upstream `fiber.spec.ts` and `isolate.spec.ts` cases that do not need the
dependents guard.
**Files**: `fiber.py`, `reflect.py`, `registry.py` (~80 total), `test_fiber.py`,
`test_isolate.py`
**Validation**: `uv run pytest tests/test_fiber.py tests/test_isolate.py tests/test_component.py
tests/test_dispose.py`
**Status**: ⬜ Pending

### Increment 8: Dependents guard, update, restart, replace
**Goal**: The provider waits for its dependents, the provision's disposer waits on release, and
`update` / `restart` / `RegistryService.replace` work.
**Files**: `fiber.py`, `reflect.py`, `registry.py` (~120 total), `test_update.py`,
`test_replace.py`, `test_service_dispose.py`
**Validation**: `uv run pytest tests/test_update.py tests/test_replace.py
tests/test_service_dispose.py tests/test_fiber.py`
**Status**: ⬜ Pending

### Increment 9: Traceable views
**Goal**: Def-site / use-site views with `cordis_tracker`, `isinstance`, use-site effect
attribution, and the shadow and caller introspection.
**Files**: `traceable.py` (~220), `service.py` (remove the `caller` argument of `cordis_resolve_intercept`), `test_associate.py`, `test_shadow.py`, `test_invoke.py`, `test_intercept.py` (the def-site case; the other cases read the view)
**Validation**: `uv run pytest` (the view changes effect attribution for every earlier module)
**Status**: ⬜ Pending

### Increment 10: Logger and @inject
**Goal**: `LoggerService` over the fiber logger, and the `@inject` decorator with its
method-level init hooks and inherited `inject` mappings.
**Files**: `logger.py` (~80), `registry.py` (~40), `test_logger.py`, `test_decorator.py`
**Validation**: `uv run pytest tests/test_logger.py tests/test_decorator.py`
**Status**: ⬜ Pending

### Increment 11: FramePumpScheduler, threading, host contract
**Goal**: Bounded `pump()`, thread-safe `submit`, owner-thread assertions, the introspection
contract, and the randomized invariant suite.
**Files**: `scheduler.py` (~80), `fiber.py` (introspection, ~30), `test_threading.py`,
`test_invariants.py`
**Validation**: `uv run pytest tests/test_scheduler.py tests/test_threading.py
tests/test_invariants.py` (mark each test the **unmarked slow tests** summary lists). Manual: in
`uv run python`, create a root context with `FramePumpScheduler`, `use` a component that
provides a service and one that injects it, pump, and read `fiber.state` and `get_effects()`
for both.
**Status**: ⬜ Pending

### Increment 12: First release
**Goal**: The public exports, the user guide, the completed module map and divergence table, and
tag `v0.1.0`.
**Files**: `__init__.py`, `docs/user_guide.md`, `docs/architecture.md`, `zensical.toml`,
`README.md`, `pyproject.toml`
**Validation**: `uv run pytest` (full suite, with zero `pending` ledger entries; the close-out
check that marks every test the **unmarked slow tests** summary lists); `uv run python
scripts/gen_ref_pages.py && uv run zensical build --strict` with no warnings.
**Status**: ⬜ Pending

## Code Standards

- [Architecture](../architecture.md) — design principles and the divergence table
- [Developer Guide](../developer_guide.md) — workflow, checks, slow tests, and release rules
- Source docstrings — the API reference builds from them

This spec is scaffolding — do not cite it, its filename, or its increment numbers in source,
docstrings, comments, or test names. Docstrings state the present rule, not the change that
introduced it.

## Follow-ups

Each item is a separate spec.

1. **Module reload helper**: a port of the module dependency classification of upstream
   `@cordisjs/hmr`, so that a host can reload a Python module and replace its components through
   `RegistryService.replace`.
2. **Declarative loader**: a port of the upstream loader's reconciliation of a configuration tree
   (enable, disable, and per-component config).
3. **Upstream sync**: review each later upstream revision with the `port-upstream` skill.

## Appendix A: Divergences

Each row becomes a row of the divergence table in `docs/architecture.md` in the increment that
implements it.

| ID | Kind | Upstream | pycordis | Contract | Reason and evidence |
|---|---|---|---|---|---|
| `service-access` | adaptation | `ctx.foo` through Proxy | `ctx[key]` (checked), `ctx.get(key)` (lenient), `Key[T]`, typed core-service attributes | — | Static typing; no `__getattr__` fallback |
| `async-model` | adaptation | Promise, microtasks | `Deferred` + scheduler task trampoline | — | A GUI main thread with a frame loop has no event loop |
| `async-effects` | adaptation | `AsyncIterable`, `Promise<Disposable>`, thenable `AsyncDisposable` | Async generator / coroutine that awaits `Deferred`; `EffectHandle.__await__` | — | Same semantics without asyncio |
| `fiber-wait` | adaptation | `ctx.use()` is `PromiseLike`; `fiber.await()` | `fiber.wait()` returns `Deferred[Fiber]`; `Fiber.__await__` | — | `await` is a keyword; explicit wait for sync code |
| `config-schema` | adaptation | Standard Schema | `model_validate` or callable transform | — | No runtime dependencies; pydantic-compatible |
| `hook-names` | adaptation | `Service.init`, `Context.filter`, `symbols.*`, `Context.is` | `cordis_*` attributes; `isinstance(value, Context)` | — | No symbols in Python; no new dunders |
| `error-notes` | adaptation | `composeError` string changes | `BaseException.add_note` | — | Native Python mechanism |
| `exception-group` | adaptation | `AggregateError` | `ExceptionGroup` | — | Native Python type |
| `stdlib-logging` | adaptation | `LoggerService` (reggol) | `LoggerService` over stdlib `logging` | — | Integration with host logging |
| `owner-thread` | improvement | Single-threaded runtime | Owner-thread assertion + `submit` | `REG-WELLFORMED` | Python hosts have worker threads; a call from a wrong thread fails at once instead of corrupting the registry. Test: `divergence("owner-thread")` in `test_threading.py` |
| `host-introspection` | improvement | `state`, `uid`, `parent`, `getEffects()` are public; children live on `runtime.fibers` | Also `fiber.error`; `fiber.children` as a view over the runtime's fibers | `LIFE-FAILURE` | A host status view reads the failure cause and the fiber tree without the runtime registration internals. Test: `divergence("host-introspection")` |
| `root-late-effect` | improvement | An effect registered on the root during root teardown stays, with no report | It stays and is logged at `DEBUG` with its label | `EFF-REVERT` | The host sees the one effect that root disposal does not revert. Test: `divergence("root-late-effect")` |
| `deprecated-aliases` | omission | `ctx.plugin` / `registry.plugin`, `events.dispatch` | Not ported | — | No vestigial API |
