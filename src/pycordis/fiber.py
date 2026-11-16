"""Fibers and effects.

A fiber owns the effects that a unit of work registers, and reverts them when the unit
ends. An effect runs some setup and collects the inverses of what the setup changed.
Start with `Fiber.effect` (through `Context.effect`) and `EffectHandle`.
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
from pycordis.errors import CallSite, CordisError
from pycordis.scheduler import Scheduler, spawn, start

if TYPE_CHECKING:
    from pycordis.context import Context

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
    `Context.effect`. The root fiber is always `ACTIVE`, and `dispose` on it reverts
    every effect of the root and leaves the root in use.

    Call fiber methods on the owner thread of the root context.

    Attributes:
        ctx: The context that the fiber's effects belong to.
    """

    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        self._scheduler = ctx.scheduler
        self._uid: int | None = 0
        self._state = FiberState.ACTIVE
        self._error: BaseException | None = None
        self._revision = 0
        self._inertia: Deferred[None] | None = None
        self._reverting = False
        self._disposables = DisposableList[Disposer]()
        self._runner = _Runner(
            execute=lambda: None,
            epoch="0",
            collect=self._disposables.push,
            call_site=CallSite(()),
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
    def error(self) -> BaseException | None:
        """The error that failed the fiber, or `None`."""
        return self._error

    @property
    def name(self) -> str:
        """The name of the fiber."""
        return "root"

    @property
    def logger(self) -> logging.Logger:
        """The logger of the fiber. It reports errors of the fiber's work."""
        return logging.getLogger(f"pycordis.{self.name}")

    def assert_active(self) -> None:
        """Raise `CordisError` when the fiber is disposed."""
        if self._uid is None:
            raise CordisError("cannot create effect on inactive context")

    def get_effects(self) -> list[EffectMeta]:
        """Return the metadata of the effects of this fiber, in registration order.

        An effect that another effect collected is a child of that effect, not an
        entry of this list.
        """
        return [d.meta for d in self._disposables if isinstance(d, EffectHandle)]

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

    def dispose(self) -> Deferred[None]:
        """Revert every effect of the fiber.

        On the root fiber this is `restart`: the root stays in use.
        """
        return self.restart()

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
        state = callback()
        self._state = state if state is not None else self._get_state()

    def _refresh(self) -> None:
        if self._uid is None:
            return
        self._set_epoch(f"{self._revision}")

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

        def transition() -> FiberState | None:
            if self._runner.epoch == INACTIVE:
                self._inertia = None
                return None
            self._inertia = start(self._scheduler, self._reload())
            return FiberState.LOADING

        self._update_state(transition)
