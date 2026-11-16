"""The result of work that finishes later.

`Deferred` is the one awaitable that a pycordis task can wait for. Start with
`Deferred` to complete a result by hand, and with `Deferred.from_future` to connect work
that runs on another thread.
"""

import threading
from collections.abc import Callable, Generator
from concurrent.futures import CancelledError, Future
from typing import Self

from pycordis.errors import CordisError

_PENDING = object()


class Deferred[T]:
    """A result that is not known yet. A task awaits it, and any thread completes it.

    A `Deferred` completes once, with a value (`resolve`) or with an error (`reject`).
    A task that awaits it resumes after it completes, and `await` gives the value or
    raises the error. The resumption is always a separate scheduler step, also when
    the `Deferred` is already complete, so `await Deferred.resolved(None)` is a
    one-step pause.

    Completion is safe from any thread. Completion callbacks run on the thread that
    completes the `Deferred`, so a callback must only hand work to a scheduler.

    Examples:
        ```python
        >>> result = Deferred[int]()
        >>> result.done()
        False
        >>> result.resolve(3)
        >>> result.result()
        3
        ```
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: object = _PENDING
        self._error: BaseException | None = None
        self._callbacks: list[Callable[[Self], None]] = []

    @classmethod
    def resolved(cls, value: T) -> "Deferred[T]":
        """Return a `Deferred` that is already complete with `value`."""
        deferred = cls()
        deferred.resolve(value)
        return deferred

    @classmethod
    def from_future(cls, future: Future[T]) -> "Deferred[T]":
        """Return a `Deferred` that completes when `future` completes.

        Use it for work that runs on another thread, such as a thread pool. A
        cancelled future rejects the `Deferred` with `CancelledError`.
        """
        deferred = cls()

        def complete(done: Future[T]) -> None:
            try:
                error = done.exception()
            except CancelledError as cancelled:
                deferred.reject(cancelled)
                return
            if error is None:
                deferred.resolve(done.result())
            else:
                deferred.reject(error)

        future.add_done_callback(complete)
        return deferred

    def done(self) -> bool:
        """Return `True` once the `Deferred` has a value or an error."""
        return self._value is not _PENDING

    def result(self) -> T:
        """Return the value, or raise the error. This never blocks.

        Raises:
            CordisError: If the `Deferred` is not complete.
            BaseException: The error that `reject` received.
        """
        if self._value is _PENDING:
            raise CordisError("DEFERRED_PENDING")
        if self._error is not None:
            raise self._error
        return self._value  # type: ignore[return-value]

    def resolve(self, value: T) -> None:
        """Complete the `Deferred` with `value`.

        Raises:
            CordisError: If the `Deferred` is already complete.
        """
        self._complete(value, None)

    def reject(self, error: BaseException) -> None:
        """Complete the `Deferred` with `error`.

        Raises:
            CordisError: If the `Deferred` is already complete.
        """
        self._complete(None, error)

    def add_done_callback(self, callback: Callable[[Self], None]) -> None:
        """Call `callback(self)` when the `Deferred` completes.

        If it is already complete, the callback runs at once, on the calling thread.
        Otherwise it runs on the thread that completes the `Deferred`.
        """
        with self._lock:
            if self._value is _PENDING:
                self._callbacks.append(callback)
                return
        callback(self)

    def __await__(self) -> Generator[Self, None, T]:
        yield self
        return self.result()

    def _complete(self, value: object, error: BaseException | None) -> None:
        with self._lock:
            if self._value is not _PENDING:
                raise CordisError("Deferred is already complete")
            self._error = error
            self._value = value
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(self)
