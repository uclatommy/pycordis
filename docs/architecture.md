# Architecture

## Design Principles

1. **The semantic contract decides correctness**: pycordis implements the programming model of
   the Cordis paper. The [semantic contract](#semantic-contract) states the paper's guarantees in
   Python terms, and each guarantee has tests. Code that breaks a contract property is wrong,
   also when upstream has the same code.
2. **Upstream is the reference implementation**: the Cordis core package (`packages/core`) and
   the other upstream packages that pycordis ports (`packages/loader`) at the tracked revision
   supply the observable behavior, the state names, the event names, and the error messages. The [conformance ledger](#conformance) records how each upstream test and
   symbol maps to pycordis, and an automatic check finds every upstream change that has not been
   reviewed.
3. **Python first at the surface**: where a Python feature gives a better API or a stronger
   guarantee, pycordis uses it and records the difference as a
   [divergence](#divergences-from-upstream). pycordis does not copy upstream code structure for
   its own sake.
4. **No runtime dependencies**: the library uses only the standard library.
5. **No event loop**: pycordis does not use asyncio. A scheduler that the host selects drives
   the lifecycle on one owner thread. Other threads can only submit work to the scheduler and
   complete a `Deferred`.
6. **Static typing**: services are reached through typed keys, not through dynamic attributes.
7. **Host neutrality**: the library does not know about a specific application or GUI toolkit.
   A host adapts pycordis through public contracts: the scheduler protocol, thread-safe submit,
   and read-only introspection.

## Background

The paper *A Programming Paradigm for Spatiotemporal Composability* (arXiv `2608.25512`) defines
the model. A component has **temporal composability** when the runtime can revert all of its
side effects when the component is removed. A component has **spatial composability** when it
declares the services it depends on and the runtime loads and unloads it as those services
change. The paper gives a calculus for both and proves its properties in Section 4.3. Section
4.4 extends the calculus with asynchrony, failure, configuration, and isolation. Cordis is the
reference implementation of the calculus (Section 5).

## Module Map

Each core module has the responsibilities of one upstream module, so that the
[conformance ledger](#conformance) is easy to read. Upstream `utils.ts` divides into three
modules.

| Upstream (`packages/core/src`) | pycordis (`src/pycordis/`) |
|---|---|
| `context.ts` | `context.py` |
| `fiber.ts` | `fiber.py` |
| `reflect.ts` | `reflect.py` |
| `registry.ts` | `registry.py` |
| `service.ts` | `service.py` |
| `events.ts` | `events.py` |
| `logger.ts` | `logger.py` |
| `utils.ts` | `disposables.py`, `traceable.py`, `errors.py` |
| — (Python runtime) | `keys.py`, `scheduler.py`, `deferred.py` |

The Python-runtime modules (`keys`, `scheduler`, `deferred`, `disposables`, `errors`) import no
other pycordis module except `errors`. The core modules import each other only where the
upstream modules do.

## Semantic Contract

Each row is a guarantee that pycordis keeps. The ID is stable: tests refer to it with
`@pytest.mark.contract("<ID>")`, and divergence rows refer to it. Every ID has at least one
test.

| ID | Guarantee | Paper |
|---|---|---|
| `EFF-REVERT` | Reverting a fiber's effects runs their inverses in reverse order of registration, and each revert restores the context state that its effect ran against. | Theorem 16, Theorem 68, Corollary 69 |
| `EFF-ISOLATE` | Operations at different keys are independent: the effects of different components can interleave, and the result is the same as if they ran one after the other. | Theorem 45, Theorem 47, Lemma 66 |
| `REG-WELLFORMED` | After every step, the fiber registry is well formed: a key has at most one provider in a realm, and each parent link names a live fiber. | Theorem 64 |
| `LIFE-ORDER` | A fiber starts to load only when every key it requires has a provider. A provider does not revert its effects while an installed dependent resolves a key to it. | Theorem 70 |
| `LIFE-COHERENT` | Every iteration of one load runs against one resolution of the fiber's dependencies. A change in that resolution during the load sends the fiber through unloading before it loads again. | Theorem 71 |
| `LIFE-PROGRESS` | With acyclic dependencies, every schedule reaches quiescence, and no fiber waits forever while a lifecycle step can run. | Theorem 73 |
| `LIFE-CONFLUENT` | Two schedules that make the same `use` and `dispose` calls reach the same quiescent state: the same fiber tree, the same provider for each key, and the same effects per fiber. A failed fiber is outside this guarantee. | Theorem 80 |
| `LIFE-FAILURE` | An `apply` that raises leaves its fiber with no installed effects and with the error recorded on the fiber. The error does not go to the parent or to siblings, and the fiber is not retried until its component or configuration changes. | Section 4.4, "Failure" |

## Conformance

pycordis measures its agreement with upstream at the level of behavior, in three layers:

| Layer | Question | Evidence |
|---|---|---|
| Semantic contract | Does pycordis keep the paper's guarantees? | Tests marked `contract`, including the seeded random-schedule suite |
| Upstream conformance | Does pycordis give the upstream result in each upstream test scenario? | Tests marked `upstream`, and the conformance ledger |
| Python surface | Is the API correct and clear for a Python developer? | The docstrings, the user guide, and their tests |

The conformance data is in `conformance/`:

- `upstream.json` is a generated inventory of the tracked upstream revision. It lists each
  upstream test case, each exported symbol, and the error messages, event names, and state
  names, with a hash of each item's normalized source. It holds no upstream code. The inventory
  always covers core, and it covers each other package that the `[inventory] packages` list of
  `ledger.toml` names. An ID of core has no prefix (`src/fiber.ts:Fiber._unload`,
  `fiber.spec.ts::Fiber::title`). An ID of another package starts with the package directory:
  `loader/src/config/entry.ts:Entry.update`, `loader/group.spec.ts::Group: basic
  support::initialize`, `loader/message:…`.
- `ledger.toml` has one entry for each inventory item. An entry gives the item's status
  (`pending` before review; `ported`, `adapted`, `diverged`, or `not-applicable` after it), the
  pycordis symbol that covers it (a `target`; a test case is linked by its `upstream` marker),
  the divergence ID where there is one, and the upstream hash at the time of review. A `pending`
  entry has no hash and no target. The inventory has `case`, `symbol`, `member`, `rest`,
  `message`, `event`, and `state` items. A marker ID is a string literal, because the checks
  read markers with `ast`.

A test in the suite checks this data with no network access. It fails when:

- the inventory revision differs from the revision in `README.md`;
- an inventory item has no ledger entry;
- a ledger hash differs from the inventory hash, which means that upstream changed the item
  after its review;
- a ledger entry names a pycordis symbol or test that does not exist, or a `ported` test case
  has no test with its `upstream` marker that runs;
- an upstream error message, event name, or state name does not occur in pycordis and has no
  divergence;
- a contract ID has no test, or a divergence row does not have the evidence its kind requires.

pycordis copies upstream control flow only where a contract proof depends on the exact
algorithm: the fiber state machine and the epoch digest (`LIFE-ORDER`, `LIFE-COHERENT`). For
these items the ledger entry sets `mirror = true`, and an upstream change to them is ported
with the same structure.

The tests that measure conformance are protected from automated test removal. A process that
removes tests, such as a cover-set reduction, never removes a test that carries an `upstream`,
`contract`, or `divergence` marker, or a test of the conformance checks themselves. Use
`-m "upstream or contract or divergence"` to select the marked tests. A process that merges
tests keeps one marker on the merged test for each case that it covers.

## Divergences from Upstream

A divergence is an intended difference from upstream in observable behavior or public surface.
Each row has a kind, and each kind needs its own evidence:

| Kind | Use when | Required evidence |
|---|---|---|
| `adaptation` | A Python construct replaces a TypeScript construct, and every upstream scenario gives the same result. | The affected upstream tests pass. |
| `improvement` | A Python feature gives a stronger guarantee or a clearer API, and some upstream scenario gives a different result. | The contract IDs it keeps, and a test marked `divergence("<ID>")`. |
| `correction` | Upstream breaks a contract property, and pycordis keeps the property. | The contract ID, a link to the upstream issue or pull request, and a test marked `divergence("<ID>")` that fails with the upstream behavior. |
| `omission` | pycordis does not port an upstream feature. | The reason. |

A new divergence needs the maintainer's approval before it is implemented. When an upstream
change removes the reason for a divergence, remove the divergence and its row.

| ID | Kind | Upstream | pycordis | Contract | Reason and evidence |
|---|---|---|---|---|---|
| `error-notes` | adaptation | `composeError` string changes | `BaseException.add_note` | — | Native Python mechanism |
| `async-effects` | adaptation | `AsyncIterable`, `Promise<Disposable>`, thenable `AsyncDisposable` | Async generator / coroutine that awaits `Deferred`; `EffectHandle.__await__` | — | Same semantics without asyncio |
| `fiber-wait` | adaptation | `ctx.use()` is `PromiseLike`; `fiber.await()` | `fiber.wait()` returns `Deferred[Fiber]`; `Fiber.__await__` | — | `await` is a keyword; explicit wait for sync code |
| `hook-names` | adaptation | `Service.init`, `Context.filter`, `symbols.*`, `Context.is` | `cordis_*` attributes; `isinstance(value, Context)` | — | No symbols in Python; no new dunders |
| `async-model` | adaptation | Promise, microtasks | `Deferred` + scheduler task trampoline | — | A GUI main thread with a frame loop has no event loop |
| `exception-group` | adaptation | `AggregateError` | `ExceptionGroup` | — | Native Python type |
| `event-dispatch-args` | adaptation | `emit(thisArg, name, ...args)` with the argument type deciding which is the `this` value; symbol event names; listeners run with `this` | `emit(name, *args, this=obj)`; any hashable event name; listeners never receive `this`; an internal event passes its receiver as the first argument | — | Python has no symbols and no way to tell a `this` value from an event name by type |
| `service-access` | adaptation | `ctx.foo` through Proxy; `internal/get` and `internal/set` listeners receive an `error` placeholder | `ctx[key]` (checked in a component's context, unchecked at the root), `ctx.get(key)` (lenient), `Key[T]`, accessor callbacks `get(ctx)` and `set(ctx, value)`; `internal/get` and `internal/set` listeners receive no `error` argument; a `RegistryService` method takes the calling context as its first argument | — | Static typing; no `__getattr__` fallback; the placeholder exists only to build a JavaScript stack |
| `config-schema` | adaptation | `Component.Config` is a Standard Schema | `Config` is an object with `model_validate` or a function from the raw config to the validated config; a failure becomes `ValidationError` | — | No runtime dependencies; pydantic-compatible |
| `traceable-view` | adaptation | `getTraceable` returns a `Proxy` of the service. The proxy shows the use-site context as `ctx`, runs methods with a shadow `this`, and is callable when the service has `symbols.invoke`. `Service[Symbol.hasInstance]` and `createCallable` serve the proxy | `get_traceable` returns a forwarding view (`TraceableView`). A method runs with a shadow object as `self`. `isinstance` works through `__class__`. A service class that defines `cordis_invoke` is callable through `__call__`. `cordis_extend` creates an extension that reads from its parent | — | Python has no `Proxy`. The view gives the same results in the upstream scenarios, and a name that starts with `cordis_` is the port of a symbol-keyed property |
| `update-result` | adaptation | `Fiber.update` returns `undefined` when an `internal/update` listener takes the update, and a `Promise` otherwise | `Fiber.update` always returns a `Deferred[None]`; it is already resolved when a listener takes the update | — | A task can await only a `Deferred`, so a result that may be `None` would raise `TypeError` in a task. The affected upstream tests pass |
| `stdlib-logging` | adaptation | `LoggerService` over its own `Logger`, `Message`, exporters with a level table, and a buffer exporter; the default name is `hyphenate(fiber.name)`; a logger takes its level from the exporter or the intercept; an error with a `cause` logs the cause too | `LoggerService` over `logging`: a `Logger` is a `LoggerAdapter` for `pycordis.<name>` that adds the fiber to each record; an exporter is a `logging.Handler` that sees only the records of its root context; `buffer` keeps the newest `LogRecord` objects of the root context; the default name is `fiber.name`; the host sets levels with `logging`; an `ExceptionGroup` logs its members | — | Integration with host logging, with no second logging system. The upstream cases pass after the host sets the level of `pycordis`. The library adds no handler to `pycordis`: each `Logger` carries a filter that fills the buffers, so the default `logging` output for a host without handlers stays |
| `logger-format` | omission | `Logger.format`, `defaultFormatters`, `Formatter`, `LoggerLevel`, `LoggerOptions`, the exporter fields `levels`, `formatters`, and `maxLength`, and the `level` of the intercept configuration | Not ported | — | `logging.Formatter`, the `logging` levels, and `logging` filters give the same function. `ColorFormatter` gives the colors |
| `root-late-effect` | improvement | An effect registered on the root during root teardown stays, with no report | It stays and is logged at `DEBUG` with its label | `EFF-REVERT` | The host sees the one effect that root disposal does not revert. Test: `divergence("root-late-effect")` |
| `owner-thread` | improvement | A single-threaded runtime with no thread check | Every `Context`, `Fiber`, and core-service method raises `CordisError("WRONG_THREAD")` when another thread calls it; `Scheduler.submit` and `Deferred` completion are allowed from any thread | `REG-WELLFORMED` | A Python host has worker threads. A call from the wrong thread fails at once and does not corrupt the registry. Test: `divergence("owner-thread")` in `tests/test_threading.py` |
| `host-introspection` | improvement | `state`, `uid`, `parent`, and `getEffects()` are public; the children of a fiber are in `runtime.fibers` | `fiber.error` and `fiber.children` are also public | `LIFE-FAILURE` | A host status view reads the failure cause and the fiber tree without the internals of the registry. Test: `divergence("host-introspection")` in `tests/test_fiber.py` |
| `error-code` | omission | `CordisError(code, message?)`: `code` keys a message table that has one entry, and the instance keeps `code` | `CordisError(message)`: the instance has no `code` | — | The message states the rule, and a Python caller catches `CordisError` and reads the message. No upstream case reads `code` |
| `deprecated-aliases` | omission | `ctx.plugin` / `registry.plugin`, `events.dispatch` | Not ported | — | No vestigial API |

A change that adds a divergence adds its row in the same change.
