"""Logging for fibers and components, on top of the standard `logging` module.

Every message goes through a standard logger named `pycordis.<name>`, so a host
configures levels, handlers, and formatters with `logging` as it does for any other
library. A `Logger` adds the fiber that logs to each record, and `LoggerService` selects
the name and the fiber for a caller. Start with `Context.logger`.

The module adds three things that `logging` does not have:

- `LoggerService.exporter` attaches a handler that receives only the records of one
  root context.
- `LoggerService.buffer` keeps the newest records of a root context in memory.
- `ColorFormatter` gives each logger name its own terminal color.
"""

import logging
from typing import TYPE_CHECKING, Any, TypedDict

from pycordis.service import Service
from pycordis.traceable import Tracker

if TYPE_CHECKING:
    from pycordis.context import Context
    from pycordis.fiber import EffectHandle, Fiber

PACKAGE_LOGGER = logging.getLogger("pycordis")
"""The logger that every `pycordis.<name>` logger propagates to."""

_FIBER_ATTRIBUTE = "cordis_fiber"

_COLORS_16 = (6, 2, 3, 4, 5, 1)
_COLORS_256 = (
    *(20, 21, 26, 27, 32, 33, 38, 39, 40, 41, 42, 43, 44, 45, 56, 57, 62),
    *(63, 68, 69, 74, 75, 76, 77, 78, 79, 80, 81, 92, 93, 98, 99, 112, 113),
    *(129, 134, 135, 148, 149, 160, 161, 162, 163, 164, 165, 166, 167, 168),
    *(169, 170, 171, 172, 173, 178, 179, 184, 185, 196, 197, 198, 199, 200),
    *(201, 202, 203, 204, 205, 206, 207, 208, 209, 214, 215, 220, 221),
)


def color_code(name: str, colors: int = 3) -> int | None:
    """Return the terminal color number that belongs to the logger `name`.

    The same name always gives the same number.

    Args:
        name: The logger name.
        colors: `0` for no color, `1` for the 16-color palette, `2` or more for the
            256-color palette.

    Returns:
        The color number, or `None` when `colors` is `0`.
    """
    if not colors:
        return None
    hash_ = 0
    units = name.encode("utf-16-le", "surrogatepass")
    for index in range(0, len(units), 2):
        unit = units[index] | (units[index + 1] << 8)
        hash_ = (hash_ << 3) - hash_ + unit + 13
        hash_ = (hash_ + 2**31) % 2**32 - 2**31
    palette = _COLORS_256 if colors >= 2 else _COLORS_16
    return palette[abs(hash_) % len(palette)]


def colorize(value: object, code: int | None) -> str:
    """Return `value` as text in the terminal color `code`.

    Args:
        value: The value to show.
        code: A number from `color_code`, or `None` for no color.
    """
    if code is None:
        return str(value)
    selector = str(code) if code < 8 else f"8;5;{code}"
    return f"\x1b[3{selector}m{value}\x1b[0m"


class ColorFormatter(logging.Formatter):
    """A `logging.Formatter` that can color the logger name.

    The format string can use `%(colored_name)s`, which is the logger name in the
    color that `color_code` gives it. Everything else is the behavior of
    `logging.Formatter`.

    Args:
        fmt: The format string.
        datefmt: The date format.
        colors: `0` for no color, `1` for 16 colors, `2` or more for 256 colors.

    Examples:
        ```python
        >>> handler = logging.StreamHandler()
        >>> handler.setFormatter(ColorFormatter("%(colored_name)s %(message)s"))
        ```
    """

    def __init__(
        self, fmt: str | None = None, datefmt: str | None = None, colors: int = 3
    ) -> None:
        super().__init__(fmt, datefmt)
        self.colors = colors

    def format(self, record: logging.LogRecord) -> str:
        """Format `record` with `colored_name` set."""
        record.colored_name = colorize(
            record.name, color_code(record.name, self.colors)
        )
        return super().format(record)


class _Buffer:
    """The newest records of one root context, oldest first."""

    def __init__(self, size: int = 1000) -> None:
        self.records: list[logging.LogRecord] = []
        self.size = size

    def add(self, record: logging.LogRecord) -> None:
        """Add `record`, and drop the oldest records beyond `size`."""
        self.records.append(record)
        overflow = len(self.records) - self.size
        if overflow > 0:
            del self.records[:overflow]


class _BufferFilter(logging.Filter):
    """Copy each record of a fiber to the buffer of the root context of that fiber.

    A filter, not a handler, so that the `pycordis` logger keeps no handler of the
    library: Python prints a warning to `stderr` only when no handler exists, and the
    host alone decides about handlers. The filter passes every record on. The roots own
    their buffers, so a root that nothing refers to can be collected.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # a record from a logger that is not a fiber logger has no fiber
        fiber: Fiber | None = getattr(record, _FIBER_ATTRIBUTE, None)
        if fiber is not None:
            fiber.ctx.root._logger._buffer.add(record)
        return True


_BUFFER_FILTER = _BufferFilter()


class _RootFilter(logging.Filter):
    """Pass only the records of fibers that belong to one root context."""

    def __init__(self, root: "Context") -> None:
        super().__init__()
        self._root = root

    def filter(self, record: logging.LogRecord) -> bool:
        # a record from a logger that is not a fiber logger has no fiber
        fiber: Fiber | None = getattr(record, _FIBER_ATTRIBUTE, None)
        return fiber is not None and fiber.ctx.root is self._root


class Logger(logging.LoggerAdapter[logging.Logger]):
    """A standard logger that records which fiber logs.

    It has the methods of `logging.LoggerAdapter`. Each record carries the fiber as the
    attribute `cordis_fiber`, which `LoggerService.exporter` uses to select the records
    of one root context.

    A call with an exception as the only argument logs it with its traceback. An
    `ExceptionGroup` logs each of its members as a record of its own.

    Args:
        logger: The standard logger that receives the records.
        fiber: The fiber that logs.

    Examples:
        ```python
        >>> ctx.logger("worker").warning("queue is full")
        ```
    """

    def __init__(self, logger: logging.Logger, fiber: "Fiber") -> None:
        super().__init__(logger, {_FIBER_ATTRIBUTE: fiber})
        logger.addFilter(_BUFFER_FILTER)  # `addFilter` ignores a filter that is present

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        """Add the fiber to the `extra` of the record."""
        assert self.extra is not None
        kwargs["extra"] = {**self.extra, **kwargs.get("extra", {})}
        return msg, kwargs

    def log(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log `msg` at `level`. See `logging.LoggerAdapter.log`."""
        if not args and isinstance(msg, BaseException):
            if isinstance(msg, BaseExceptionGroup):
                for member in msg.exceptions:
                    self.log(level, member, **kwargs)
                return
            kwargs.setdefault("exc_info", msg)
        super().log(level, msg, *args, **kwargs)


class LoggerIntercept(TypedDict, total=False):
    """The intercept configuration of the logger service.

    Attributes:
        name: The logger name that a call without a name uses.
    """

    name: str


class LoggerService(Service[LoggerIntercept]):
    """The logging service of a root context. Read it as `Context.logger`.

    Call it to get a `Logger`, or call `debug`, `info`, `warning`, and `error` on it
    directly. The name of the logger is, in this order: the argument, the `name` of an
    intercept configuration (`Context.intercept("logger", {"name": ...})`, or the
    `inject` configuration of the fiber), and the name of the fiber of the caller. The
    logger is `logging.getLogger("pycordis.<name>")`, so the host sets its level with
    `logging`; this service sets no level.

    The service keeps the newest records of its root context in `buffer`, as long as
    the root context exists. The library adds no handler: a `Logger` copies each record
    to the buffer before the handlers of the host see it.

    Attributes:
        ctx: The root context.
        buffer: The newest records of the root context, oldest first. The list object
            stays the same.
        buffer_size: The most records that `buffer` keeps. The default is 1000.

    Examples:
        ```python
        >>> logging.getLogger("pycordis").setLevel(logging.INFO)
        >>> ctx.logger("db").info("connected")
        >>> ctx.logger.buffer[-1].getMessage()
        'connected'
        ```
    """

    def __init__(self, ctx: "Context") -> None:
        # The service is not provided under a name: `Context.logger` reads it. It
        # takes no part in `inject`, and does not call `Service.__init__`.
        self.ctx = ctx
        self.name = "logger"
        self.cordis_tracker = Tracker(property="ctx", no_shadow=True)
        self._buffer = _Buffer()

    @property
    def buffer(self) -> list[logging.LogRecord]:
        """The newest records of the root context, oldest first."""
        return self._buffer.records

    @property
    def buffer_size(self) -> int:
        """The most records that `buffer` keeps."""
        return self._buffer.size

    @buffer_size.setter
    def buffer_size(self, size: int) -> None:
        self._buffer.size = size

    def exporter(self, handler: logging.Handler) -> "EffectHandle":
        """Send the records of this root context to `handler`.

        The handler gets the records of every fiber of the root context, at the levels
        that `logging` allows. A record of another root context, and a record from a
        logger that no fiber owns, does not reach it. The effect belongs to the fiber of
        the calling context.

        Args:
            handler: A standard handler. Give it a `ColorFormatter` for colors.

        Returns:
            The handle of the effect. Call it to detach the handler.
        """
        root_filter = _RootFilter(self.ctx.root)

        def attach() -> Any:
            handler.addFilter(root_filter)
            PACKAGE_LOGGER.addHandler(handler)

            def detach() -> None:
                PACKAGE_LOGGER.removeHandler(handler)
                handler.removeFilter(root_filter)

            return detach

        return self.ctx.effect(attach, "ctx.logger.exporter()")

    def cordis_invoke(self, name: str | None = None) -> Logger:
        """Return the logger for `name`, or for the caller when `name` is `None`."""
        if name is None:
            name = self.cordis_resolve_intercept().get("name")
        fiber = self.cordis_caller.fiber if self.cordis_caller else self.ctx.fiber
        if name is None:
            name = fiber.name
        return Logger(logging.getLogger(f"pycordis.{name}"), fiber)

    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log at `DEBUG` through the logger of the caller."""
        self().debug(msg, *args, **kwargs)

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log at `INFO` through the logger of the caller."""
        self().info(msg, *args, **kwargs)

    def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log at `WARNING` through the logger of the caller."""
        self().warning(msg, *args, **kwargs)

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log at `ERROR` through the logger of the caller."""
        self().error(msg, *args, **kwargs)
