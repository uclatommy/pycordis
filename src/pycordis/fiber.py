"""Fibers and effects.

A fiber owns the effects that a unit of work registers, and reverts them when the unit
ends. An effect runs some setup and collects the inverses of what the setup changed.
A component runs in its own fiber, which loads when every service that the component
injects has a provider. Start with `Fiber.effect` (through `Context.effect`),
`EffectHandle`, and `FiberState`.
"""

import inspect
import logging
from collections.abc import (
    AsyncIterator,
    Callable,
    Coroutine,
    Generator,
    Iterator,
)
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Self

from pycordis.deferred import Deferred
from pycordis.disposables import DisposableList
from pycordis.errors import CallSite, CordisError, Issue, ValidationError
from pycordis.scheduler import Scheduler, owner_thread, spawn, start
from pycordis.traceable import get_traceable

if TYPE_CHECKING:
    from pycordis.context import Context
    from pycordis.logger import Logger
    from pycordis.reflect import Impl
    from pycordis.registry import Runtime

type Disposer = Callable[[], Deferred[None] | None]
"""A function that reverts one change. It returns a `Deferred` if it ends later."""

type EffectForm = (
    Disposer
    | Iterator[Disposer | None]
    | AsyncIterator[Disposer | None]
    | Coroutine[Any, Any, Disposer | None]
    | None
)
"""What the setup function of an effect returns.

`None` registers no inverse. A disposer is the inverse. An iterator or an async
iterator yields one inverse at a time. A coroutine returns one inverse when it ends.
"""

INACTIVE = "__INACTIVE__"
"""The epoch of a fiber that cannot run."""


class FiberState(IntEnum):
    """The lifecycle state of a fiber."""

    PENDING = 0
    LOADING = 1
    ACTIVE = 2
    FAILED = 3
    DISPOSED = 4
    UNLOADING = 5


@dataclass(slots=True)
class EffectMeta:
    """The label of an effect and the effects that it collected.

    Attributes:
        label: The name that the effect was created with.
        children: The metadata of the effect handles that the setup returned or
            yielded, in order.
    """

    label: str
    children: list["EffectMeta"] = field(default_factory=list)


@dataclass(slots=True)
class _Runner:
    """What `Fiber._execute` needs to run one setup and to collect its inverses."""

    execute: Callable[[], EffectForm]
    epoch: object
    collect: Callable[[Disposer], object]
    call_site: CallSite


def _issues(error: Exception) -> list[Issue]:
    """Read the issues of a validator error from its `errors()` method, if any."""
    errors = getattr(error, "errors", None)
    if not callable(errors):
        return [Issue(str(error))]
    return [Issue(str(item["msg"]), tuple(item.get("loc", ()))) for item in errors()]


def resolve_config(schema: object | None, config: object) -> object:
    """Validate `config` with the `Config` of a component, and return the result.

    Args:
        schema: An object with a `model_validate` method, a function that takes the raw
            config and returns the validated one, or `None` for no validation.
        config: The raw config.

    Returns:
        The config that the validator returns, or `config` when `schema` is `None`.

    Raises:
        ValidationError: If the validator raises. When the error has an `errors()`
            method, each entry gives one issue with its `msg` and `loc`.
        TypeError: If `schema` is neither an object with `model_validate` nor a
            function, or if the validator returns an awaitable.
    """
    if schema is None:
        return config
    validate = getattr(schema, "model_validate", schema)
    if not callable(validate):
        raise TypeError("Config must have a model_validate method or be callable")
    try:
        result = validate(config)
    except ValidationError:
        raise
    except Exception as error:
        raise ValidationError(_issues(error)) from error
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise TypeError("Async config validation is not supported")
    return result


def _hop() -> Deferred[None]:
    """Return a `Deferred` that gives one scheduler step when awaited."""
    return Deferred.resolved(None)


async def _then(task: Deferred[None], fn: Callable[[], Deferred[None] | None]) -> None:
    """Wait for `task`, then run `fn` and wait for its result."""
    await task
    result = fn()
    if result is not None:
        await result


async def _noted(site: CallSite, body: Coroutine[Any, Any, None]) -> None:
    """Run `body`, and add `site` to the error that it raises."""
    try:
        await body
    except Exception as error:
        site.annotate(error, "Effect created at")
        raise


def _collect(runner: _Runner, value: object) -> None:
    if callable(value):
        runner.collect(value)
    elif value is not None:
        raise TypeError("Invalid effect")


def _run_iterator(runner: _Runner, iterator: Iterator[Any]) -> None:
    try:
        while True:
            try:
                value = next(iterator)
            except StopIteration as stop:
                _collect(runner, stop.value)
                return
            _collect(runner, value)
    except BaseException:
        # run the `finally` blocks of an iterator that the loop abandons
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
        raise


async def _run_coroutine(runner: _Runner, coroutine: Coroutine[Any, Any, Any]) -> None:
    _collect(runner, await coroutine)


async def _run_async_iterator(
    runner: _Runner, iterator: AsyncIterator[Any], epoch: object
) -> None:
    exhausted = False
    try:
        while runner.epoch == epoch:
            try:
                value = await anext(iterator)
            except StopAsyncIteration:
                exhausted = True
                return
            _collect(runner, value)
    finally:
        # run the `finally` blocks of an iterator that the loop abandons
        close = getattr(iterator, "aclose", None)
        if not exhausted and close is not None:
            await close()


class EffectHandle:
    """The handle of one effect. Call it to revert the effect.

    Calling the handle runs the inverses that the effect collected, newest first, and
    waits for each inverse that returns a `Deferred` before it runs the next. A second
    call does nothing. The call returns a `Deferred` when an inverse finishes later,
    or when the setup is still running: the inverses then run when the setup ends.

    The fiber that created the effect owns it. The fiber reverts the effect when it
    unloads, whether or not the program keeps the handle.

    A task can `await` the handle. The `await` waits until the setup has finished, and
    gives a disposer that reverts the effect at once. It raises the error of a failed
    setup.

    Examples:
        ```python
        >>> handle = ctx.effect(lambda: lambda: print("reverted"), "demo")
        >>> handle()
        reverted
        ```

    Attributes:
        meta: The label of the effect and the metadata of its child effects.
    """

    def __init__(
        self,
        scheduler: Scheduler,
        meta: EffectMeta,
        runner: _Runner,
        dispose: Disposer,
        task: Deferred[None] | None,
    ) -> None:
        self.meta = meta
        self._scheduler = scheduler
        self._runner = runner
        self._dispose = dispose
        self._task = task

    def __call__(self) -> Deferred[None] | None:
        if not self._runner.epoch:
            return None
        self._runner.epoch = False
        with self._scheduler.call():
            if self._task is None:
                return self._dispose()
            return spawn(self._scheduler, _then(self._task, self._dispose))

    def __await__(self) -> Generator[Deferred[None], None, Disposer]:
        yield from (self._task or _hop()).__await__()
        return self._revert

    def _revert(self) -> Deferred[None] | None:
        if not self._runner.epoch:
            return None
        self._runner.epoch = False
        with self._scheduler.call():
            return self._dispose()


class Fiber:
    """A unit of work that owns effects and reverts them when it ends.

    A fiber has a lifecycle state (`FiberState`). Register effects on it through
    `Context.effect`. `Context.use` runs a component in a new fiber: the fiber loads
    when every service in `inject` has a provider that is `ACTIVE`, reverts its
    effects when one of them changes, and loads again against the new provider.

    The root fiber is always `ACTIVE`, and `dispose` on it reverts every effect of the
    root and leaves the root in use.

    Call fiber methods on the owner thread of the root context. A call from another
    thread raises `CordisError("WRONG_THREAD")`.

    Attributes:
        parent: The context that created the fiber.
        ctx: The context that the fiber's effects belong to.
        config: The config that the component receives.
        inject: The services that the fiber requires, by name. The value is the
            intercept configuration, or `None`.
        runtime: The record of the fiber's component, or `None` for the root fiber.
        committed: The providers that the running load resolved, by name, or `None`
            while the fiber is not loaded.
        provisions: The services that the fiber provides.
        update_hooks: The `internal/update` listeners that the fiber registered.
    """

    def __init__(
        self,
        parent: "Context",
        config: object = None,
        inject: dict[str, object] | None = None,
        runtime: "Runtime | None" = None,
        call_site: CallSite | None = None,
    ) -> None:
        self.parent = parent
        self.config = config
        self.inject: dict[str, object] = {} if inject is None else inject
        self.runtime = runtime
        self.committed: dict[str, Impl] | None = None
        self.provisions: set[Impl] = set()
        self._scheduler = parent.scheduler
        self._error: BaseException | None = None
        self._revision = 0
        self._inertia: Deferred[None] | None = None
        self._reverting = False
        self._disposables = DisposableList[Disposer]()
        self.update_hooks = DisposableList[Callable[..., Any]]()
        self._store: dict[str, Impl] = {}
        self._children: dict[Fiber, None] = {}
        self._handle: EffectHandle | None = None
        self._release: Callable[[], None] | None = None
        if runtime is None:
            self._uid: int | None = 0
            self._state = FiberState.ACTIVE
            self.ctx = parent
            self.committed = {}
            self._runner = _Runner(
                execute=lambda: None,
                epoch="0",
                collect=self._disposables.push,
                call_site=CallSite(()),
            )
            return
        self._uid = parent.registry.next_uid()
        self._state = FiberState.PENDING
        self.ctx = parent.bind_fiber(self)
        self._runner = _Runner(
            execute=self._apply,
            epoch=INACTIVE,
            collect=self._disposables.push,
            call_site=call_site if call_site is not None else CallSite(()),
        )
        self.ctx.emit("internal/plugin", self)
        for name in self.inject:
            self._check_impl(name)
        self._handle = parent.fiber.effect(
            self._attach, "ctx.use()", call_site=call_site
        )

    @property
    def uid(self) -> int | None:
        """The number of the fiber: `0` for the root, `None` once disposed."""
        return self._uid

    @property
    def state(self) -> FiberState:
        """The lifecycle state of the fiber."""
        return self._state

    @property
    def children(self) -> tuple["Fiber", ...]:
        """The live fibers that this fiber's context created, in creation order.

        A child leaves the tuple when it is disposed. A child that reloads or moves to
        a replacement component keeps its place. The tuple is a snapshot.
        """
        return tuple(self._children)

    @property
    def error(self) -> BaseException | None:
        """The error that failed the fiber, or `None`."""
        return self._error

    @property
    def name(self) -> str:
        """The name of the fiber.

        It is the name of the fiber's component. A fiber whose component has no name
        takes the name of the nearest ancestor that has one, or `"root"`.
        """
        fiber = self
        while True:
            if fiber.runtime is not None and fiber.runtime.name:
                return fiber.runtime.name
            parent = fiber.parent.fiber
            if parent is fiber:
                return "root"
            fiber = parent

    @property
    def logger(self) -> "Logger":
        """The logger of the fiber. It reports errors of the fiber's work."""
        from pycordis.logger import Logger  # logger.py imports the service layer

        return Logger(logging.getLogger(f"pycordis.{self.name}"), self)

    def assert_active(self) -> None:
        """Raise `CordisError` when the fiber is disposed."""
        if self._uid is None:
            raise CordisError("cannot create effect on inactive context")

    @owner_thread("_scheduler")
    def get_effects(self) -> list[EffectMeta]:
        """Return the metadata of the effects of this fiber, in registration order.

        An effect that another effect collected is a child of that effect, not an
        entry of this list.
        """
        return [d.meta for d in self._disposables if isinstance(d, EffectHandle)]

    @owner_thread("_scheduler")
    def effect(
        self,
        execute: Callable[[], EffectForm],
        label: str = "anonymous",
        *,
        call_site: CallSite | None = None,
    ) -> EffectHandle:
        """Run `execute` and keep the inverses that it returns.

        The setup function returns `None`, a disposer, a sync iterator of disposers,
        an async iterator of disposers, or a coroutine that returns a disposer. Any
        other value raises `TypeError("Invalid effect")`. A task runs an async iterator
        or a coroutine, so it can await a `Deferred`. The async iterator stops at its
        next step after the effect is reverted, and its `aclose()` runs.

        When the setup raises, the inverses that it collected run, and the error
        goes to the caller with the call site as a note. An async setup reports its
        error through `await handle`.

        Args:
            execute: The setup function.
            label: The name that `get_effects` reports.
            call_site: Where the effect was created. The default is the caller.

        Returns:
            The handle of the effect.

        Raises:
            CordisError: If the fiber is disposed.
            TypeError: If `execute` returns an invalid effect.
        """
        self.assert_active()
        site = call_site if call_site is not None else CallSite.capture(skip=1)
        scheduler = self._scheduler
        inverses: list[Disposer] = []
        meta = EffectMeta(label)

        def collect(inverse: Disposer) -> None:
            inverses.append(inverse)
            self._disposables.delete(inverse)
            if isinstance(inverse, EffectHandle):
                meta.children.append(inverse.meta)

        def dispose() -> Deferred[None] | None:
            pending: Deferred[None] | None = None
            while inverses:
                inverse = inverses.pop()
                if pending is not None:
                    pending = spawn(scheduler, _then(pending, inverse))
                    continue
                result = inverse()
                if result is not None:
                    pending = result
            return pending

        runner = _Runner(execute, True, collect, site)
        with scheduler.call():
            try:
                task = self._execute(runner)
            except BaseException:
                self._log_failure(dispose(), label)
                raise
            if task is not None:
                self._revert_on_failure(task, dispose, label)
            handle = EffectHandle(scheduler, meta, runner, dispose, task)
            inverses.append(self._disposables.push(handle))
        if self._reverting and self._uid == 0:
            # EFF-REVERT, root-late-effect: this effect outlives the disposal that is
            # running.
            self.logger.debug(
                "effect %r registered during root disposal is not reverted", label
            )
        return handle

    @owner_thread("_scheduler")
    def wait(self) -> Deferred[Self]:
        """Wait until no load or unload is in progress.

        Returns:
            A `Deferred` that resolves with the fiber, or rejects with the error that
            failed it.
        """
        with self._scheduler.call():
            return start(self._scheduler, self._settle())

    def __await__(self) -> Generator[Deferred[Self], None, Self]:
        return self.wait().__await__()

    @owner_thread("_scheduler")
    def restart(self) -> Deferred[None]:
        """Revert the effects of the fiber and run it again.

        Returns:
            A `Deferred` that resolves when the fiber is idle again, or rejects with
            the error that failed it.
        """
        with self._scheduler.call():
            self.assert_active()
            self._revision += 1
            self._refresh()
            return start(self._scheduler, self._restarted())

    @owner_thread("_scheduler")
    def update(self, config: object, no_save: bool = False) -> Deferred[None]:
        """Give the fiber a new config, and run the component again with it.

        A loaded fiber first validates `config` with the component's `Config`, and an
        invalid config raises before anything changes. The fiber then stores `config`,
        clears the error of a failed fiber, and calls the `internal/update` listeners
        with `(fiber, resolved_config, next)`. Calling `next()` goes on to the next
        listener, and the last `next()` restarts the fiber. A listener that does not
        call `next()` takes the update itself: the fiber keeps running with its old
        effects, and the new config applies at the next restart.

        A fiber that is not loaded (it waits for a service, or it failed) skips the
        listeners and restarts at once. A failed fiber loads again with the new config.

        Several updates while a load or unload runs end in one load with the latest
        config, and each returned `Deferred` resolves when that load ends.

        Args:
            config: The raw config.
            no_save: Do not emit `internal/commit`. A host that stores the configs
                of its components listens to that event to save them.

        Returns:
            A `Deferred` that resolves when the fiber is idle again, or rejects with
            the error that failed the fiber. It is already resolved when a listener
            takes the update. The fiber logs the failure itself, so a caller can
            drop the `Deferred`.

        Raises:
            ValidationError: If the fiber is loaded and `config` is not valid.
            CordisError: If the fiber is disposed.

        Examples:
            ```python
            >>> fiber = ctx.use(lambda ctx, config: print("config", config), 1)
            config 1
            >>> _ = fiber.update(2)
            config 2
            ```
        """
        with self._scheduler.call():
            self.assert_active()
            loaded = self._runner.epoch != INACTIVE
            resolved = self._resolve(config) if loaded else None
            if not no_save:
                self.ctx.emit("internal/commit", self, config)
            self.config = config
            self._error = None
            if not loaded:
                return self.restart()
            result = self.ctx.waterfall("internal/update", self, resolved, self.restart)
            if isinstance(result, Deferred):
                return result
            if inspect.iscoroutine(result):
                return start(self._scheduler, result)
            return Deferred.resolved(None)

    @owner_thread("_scheduler")
    def dispose(self) -> Deferred[None]:
        """End the fiber: unload it, revert its effects, and remove it.

        The fiber goes to `DISPOSED`, and its child fibers end with it. A second call
        does nothing. On the root fiber, `dispose` is `restart`: the root stays in use.

        Returns:
            A `Deferred` that resolves when the unload has finished.
        """
        if self._handle is None:
            return self.restart()
        result = self._handle()
        return Deferred.resolved(None) if result is None else result

    async def _restarted(self) -> None:
        await self._settle()

    async def _settle(self) -> Self:
        while self._inertia is not None:
            await self._inertia
        if self._error is not None:
            raise self._error
        return self

    def _execute(self, runner: _Runner) -> Deferred[None] | None:
        epoch = runner.epoch
        scheduler = self._scheduler
        try:
            effect = runner.execute()
            if effect is None:
                return None
            if callable(effect):
                runner.collect(effect)
            elif inspect.iscoroutine(effect):
                return spawn(
                    scheduler, _noted(runner.call_site, _run_coroutine(runner, effect))
                )
            elif isinstance(effect, Iterator):
                _run_iterator(runner, effect)
            elif isinstance(effect, AsyncIterator):
                return spawn(
                    scheduler,
                    _noted(
                        runner.call_site, _run_async_iterator(runner, effect, epoch)
                    ),
                )
            else:
                raise TypeError("Invalid effect")
        except Exception as error:
            runner.call_site.annotate(error, "Effect created at")
            raise
        return None

    def _revert_on_failure(
        self,
        task: Deferred[None],
        dispose: Callable[[], Deferred[None] | None],
        label: str,
    ) -> None:
        """Revert what a failed setup collected. The error stays in `task`."""

        async def revert() -> None:
            try:
                await task
            except Exception:
                try:
                    result = dispose()
                    if result is not None:
                        await result
                except Exception as error:
                    self._log_error(label, error)

        spawn(self._scheduler, revert())

    def _log_failure(self, pending: Deferred[None] | None, label: str) -> None:
        if pending is not None:
            spawn(self._scheduler, self._logged(pending, label))

    def _log_error(self, label: str, error: BaseException) -> None:
        self.logger.error("error while reverting effect %r", label, exc_info=error)

    async def _logged(self, pending: Deferred[None], label: str) -> None:
        """Wait for `pending`, and log its error instead of raising it."""
        try:
            await pending
        except Exception as error:
            self._log_error(label, error)

    def _apply(self) -> EffectForm:
        """Run the component of the fiber with its resolved config."""
        assert self.runtime is not None
        callback = self.runtime.callback
        config = self._resolve(self.config)
        if not inspect.isclass(callback):
            return callback(self.ctx, config)
        instance = callback(self.ctx, config)
        self.ctx.registry.run_inject_methods(self.ctx, instance)
        init = getattr(instance, "cordis_init", None)
        return None if init is None else init()

    def _resolve(self, config: object) -> Any:
        return self.ctx.waterfall("internal/config", self, lambda: config)

    def _attach(self) -> Disposer:
        """Register the fiber with its runtime and start its load.

        The effect of the parent fiber that runs this method owns the fiber: the
        returned disposer ends it.
        """
        assert self.runtime is not None
        self.runtime.fibers.push(self)
        self.parent.fiber._children[self] = None
        try:
            self._refresh()
        except Exception as error:
            self.logger.error("error while loading", exc_info=error)
            self._error = error
        return self._leave

    def _leave(self) -> Deferred[None]:
        with self._scheduler.call():
            return start(self._scheduler, self._left())

    async def _left(self) -> None:
        assert self.runtime is not None
        self._uid = None
        self.ctx.emit("internal/plugin", self)
        runtime = self.runtime
        del self.parent.fiber._children[self]
        runtime.fibers.delete(self)
        if not len(runtime.fibers):
            self.ctx.registry.unregister(runtime)
        self._set_epoch(INACTIVE)
        # `_reload` and `_unload` log their own errors, so `_inertia` never rejects
        while self._inertia is not None:
            await self._inertia
        # `_set_epoch` returns early for a fiber that was not ACTIVE, so the state is
        # derived again now that the uid is gone
        self._update_state(lambda: None)

    # The lifecycle below keeps the structure of the fiber state machine: the epoch
    # decides every transition (LIFE-ORDER, LIFE-COHERENT).

    def _get_state(self) -> FiberState:
        if self._uid is None:
            return FiberState.DISPOSED
        if self._error is not None:
            return FiberState.FAILED
        if self._runner.epoch != INACTIVE:
            return FiberState.ACTIVE
        return FiberState.PENDING

    def _update_state(self, callback: Callable[[], FiberState | None]) -> None:
        old_state = self._state
        state = callback()
        self._state = state if state is not None else self._get_state()
        if old_state == self._state:
            return
        self.ctx.emit("internal/status", self, old_state)
        # only a change between ACTIVE and a state that is not ACTIVE is a change of
        # the providers
        if old_state != FiberState.ACTIVE and self._state != FiberState.ACTIVE:
            return
        self.ctx.reflect.announce(self)

    def _check_impl(self, name: str) -> None:
        impl = self.ctx.reflect.find(self.ctx, name, True)
        if impl is not None and impl.check is not None:
            try:
                accepted = impl.check(get_traceable(self.ctx, impl.value))
            except Exception as error:
                impl.ctx.fiber.logger.error(
                    "error while checking service %r", name, exc_info=error
                )
                accepted = False
            if not accepted:
                impl = None
        if impl is None:
            self._store.pop(name, None)
        else:
            self._store[name] = impl

    def _replace(self, runtime: "Runtime", inject: dict[str, object]) -> None:
        """Move the fiber to `runtime` and `inject`, and run it again.

        The registry calls it for each live fiber of a replaced component. The fiber
        keeps its identity. A failed fiber recovers, as it does through `update`.
        """
        if runtime is not self.runtime:
            assert self.runtime is not None
            self.runtime.fibers.delete(self)
            runtime.fibers.push(self)
            self.runtime = runtime
        self.inject = inject
        self.ctx.emit("internal/replace", self)
        self._store = {}
        for name in self.inject:
            self._check_impl(name)
        self._error = None
        self._revision += 1
        self._refresh()

    def _refresh(self) -> None:
        if self._uid is None:
            return
        epoch = f"{self._revision}"
        for name in self.inject:
            impl = self._store.get(name)
            if impl is None:
                epoch = INACTIVE
                break
            epoch += f":{impl.uid}"
        self._set_epoch(epoch)

    def _set_epoch(self, epoch: str) -> None:
        old_epoch = self._runner.epoch
        if epoch == old_epoch:
            return
        # a failed fiber only recovers through update(), which clears the error
        if self._error is not None:
            return
        self._runner.epoch = epoch
        if self._inertia is not None:
            return

        def transition() -> FiberState:
            if epoch != INACTIVE and old_epoch == INACTIVE:
                self._inertia = start(self._scheduler, self._reload())
                return FiberState.LOADING
            self._inertia = start(self._scheduler, self._unload())
            return FiberState.UNLOADING

        self._update_state(transition)

    async def _reload(self) -> None:
        self.committed = dict(self._store)
        for impl in self.committed.values():
            if impl.ctx.fiber is self:
                continue
            impl.dependents.add(self)
        old_epoch = self._runner.epoch
        try:
            await _hop()
            if self._runner.epoch == old_epoch:
                task = self._execute(self._runner)
                if task is not None:
                    await task
        except Exception as reason:
            self.logger.error("error while loading", exc_info=reason)
            # a superseded load still needs cleanup, but cannot fail the new target
            if self._runner.epoch == old_epoch:
                self._error = reason
                self._runner.epoch = INACTIVE

        def transition() -> FiberState | None:
            if self._runner.epoch == old_epoch:
                self._inertia = None
                return None
            self._inertia = start(self._scheduler, self._unload())
            return FiberState.UNLOADING

        self._update_state(transition)

    async def _unload(self) -> None:
        # LIFE-ORDER: dependents leave when this fiber leaves ACTIVE, but they can
        # still read its services during their own teardown, so the effects of this
        # fiber are reverted only after every dependent has unloaded.
        while any(impl.dependents for impl in list(self.provisions)):
            await self._released()
        self._release = None
        # Every unload awaits at least once, so the caller stores the task before it
        # ends. Disposers start in reverse registration order. A disposer that
        # returns a `Deferred` finishes concurrently with the others, and an error
        # does not stop the others.
        disposers = self._disposables.clear()
        disposers.reverse()
        self._reverting = True
        await _hop()
        pending: list[Deferred[None]] = []
        for dispose in disposers:
            label = (
                dispose.meta.label if isinstance(dispose, EffectHandle) else "anonymous"
            )
            try:
                result = dispose()
            except Exception as error:
                self._log_error(label, error)
                continue
            if result is not None:
                pending.append(spawn(self._scheduler, self._logged(result, label)))
        for watcher in pending:
            await watcher
        self._reverting = False
        # release the providers that wait for this fiber to unload
        for impl in (self.committed or {}).values():
            if self not in impl.dependents:
                continue
            impl.dependents.discard(self)
            if impl.dependents:
                continue
            if impl.release is not None:
                impl.release()
            release = impl.ctx.fiber._release
            if release is not None:
                release()
        self.committed = None

        def transition() -> FiberState | None:
            if self._runner.epoch == INACTIVE:
                self._inertia = None
                return None
            self._inertia = start(self._scheduler, self._reload())
            return FiberState.LOADING

        self._update_state(transition)

    def _released(self) -> Deferred[None]:
        """Return a `Deferred` that the next release of a dependent resolves."""
        gate = Deferred[None]()

        def release() -> None:
            if not gate.done():
                gate.resolve(None)

        self._release = release
        return gate
