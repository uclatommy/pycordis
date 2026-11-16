"""The logging service: names, exporters, the buffer, and colors."""

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from pycordis.context import Context
from pycordis.logger import ColorFormatter, color_code, colorize
from pycordis.service import Service


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def names(self) -> list[str]:
        return [record.name.removeprefix("pycordis.") for record in self.records]


@pytest.fixture(autouse=True)
def debug_level() -> Iterator[None]:
    package = logging.getLogger("pycordis")
    level = package.level
    package.setLevel(logging.DEBUG)
    yield
    package.setLevel(level)


@pytest.fixture
def captured(root: Context) -> Capture:
    capture = Capture()
    root.logger.exporter(capture)
    return capture


def make_service(name: str, label: str, inject: list[str] | None = None) -> type:
    class Named(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, label)

        def action(self) -> None:
            self.ctx.logger.debug("from " + label)

    Named.__name__ = name
    if inject:
        Named.inject = inject  # type: ignore[attr-defined]
    return Named


@pytest.mark.upstream(
    "logger.spec.ts::Logger::uses fiber name when called from outside any service"
)
@pytest.mark.upstream("logger.spec.ts::Logger::honours explicit name argument")
@pytest.mark.upstream("logger.spec.ts::Logger::honours intercept name")
def test_the_logger_name_is_the_argument_then_the_intercept_then_the_fiber(
    root: Context, captured: Capture
) -> None:
    root.logger.debug("a")
    root.logger("custom").debug("b")
    root.intercept("logger", {"name": "intercepted"}).logger.debug("c")
    root.intercept("logger", {"name": "intercepted"}).logger("explicit").debug("d")

    assert captured.names == ["root", "custom", "intercepted", "explicit"]


@pytest.mark.upstream(
    "logger.spec.ts::Logger::uses service name when called from inside a Service method (regression)"  # noqa: E501
)
@pytest.mark.upstream(
    "logger.spec.ts::Logger::still lets outer caller intercept override the service-derived name"  # noqa: E501
)
def test_a_service_method_logs_under_the_service_name_unless_the_caller_intercepts(
    root: Context, captured: Capture
) -> None:
    root.use(make_service("FooDriver", "foo"))
    root["foo"].action()
    root.intercept("logger", {"name": "caller-override"})["foo"].action()

    assert captured.names == ["FooDriver", "caller-override"]


@pytest.mark.upstream(
    "logger.spec.ts::Logger::uses the innermost service name and restores the outer service"  # noqa: E501
)
def test_a_nested_service_call_restores_the_outer_name(
    root: Context, captured: Capture
) -> None:
    bar = make_service("BarDriver", "bar")
    foo = make_service("FooDriver", "foo", ["bar"])

    def action(self: Any) -> None:
        self.ctx["bar"].action()
        self.ctx.logger.debug("from foo")

    foo.action = action  # type: ignore[attr-defined]
    root.use(bar)
    root.use(foo)
    root.inject(["foo"], lambda ctx: ctx["foo"].action())

    assert [(r.name.removeprefix("pycordis."), r.msg) for r in captured.records] == [
        ("BarDriver", "from bar"),
        ("FooDriver", "from foo"),
    ]


@pytest.mark.upstream(
    "logger.spec.ts::Logger::uses service name when called from inside [Service.init] (unchanged behaviour)"  # noqa: E501
)
def test_an_init_hook_logs_under_the_service_name(
    root: Context, captured: Capture
) -> None:
    class Foo(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "foo")

        def cordis_init(self) -> None:
            self.ctx.logger.debug("from init")

    root.use(Foo)

    assert captured.names == ["Foo"]


@pytest.mark.upstream(
    "logger.spec.ts::Logger::keeps the bounded buffer in place and chronological"
)
def test_the_buffer_keeps_the_newest_records_in_the_same_list(root: Context) -> None:
    buffer = root.logger.buffer
    assert root.logger.buffer_size == 1000
    root.logger.buffer_size = 2
    for text in ("one", "two", "three"):
        root.logger.info(text)
    assert root.logger.buffer is buffer
    assert [r.msg for r in buffer] == ["two", "three"]

    root.logger.buffer_size = 1
    root.logger.info("four")
    assert [r.msg for r in buffer] == ["four"]

    root.logger.buffer_size = 0
    root.logger.info("five")
    assert buffer == []


@pytest.mark.upstream(
    "logger.spec.ts::Logger::disposes the exporter that registered the disposer"
)
def test_the_handle_of_an_exporter_detaches_that_exporter(root: Context) -> None:
    first, second = Capture(), Capture()
    detach_first = root.logger.exporter(first)
    detach_second = root.logger.exporter(second)

    detach_first()
    root.logger.info("test")
    assert (len(first.records), len(second.records)) == (0, 1)

    detach_second()
    root.logger.info("test")
    assert len(second.records) == 1


def test_an_exporter_ends_with_the_fiber_that_registered_it(root: Context) -> None:
    capture = Capture()
    fiber = root.use(lambda ctx, config: ctx.logger.exporter(capture))
    root.logger.info("while loaded")
    fiber.dispose()
    root.logger.info("after")

    assert [r.msg for r in capture.records] == ["while loaded"]


def test_an_exporter_gets_only_the_records_of_its_own_root(
    root: Context, scheduler: Any
) -> None:
    other = Context(scheduler)
    mine, theirs = Capture(), Capture()
    root.logger.exporter(mine)
    other.logger.exporter(theirs)
    logging.getLogger("pycordis.host").warning("not from a fiber")

    root.logger.info("a")
    other.logger.info("b")

    assert [r.msg for r in mine.records] == ["a"]
    assert [r.msg for r in theirs.records] == ["b"]
    assert [r.msg for r in root.logger.buffer] == ["a"]


def test_the_library_adds_no_handler_to_the_package_logger(root: Context) -> None:
    # With no handler, `logging` prints a warning or an error to stderr by itself.
    root.logger.error("boom")

    # Other tests attach exporters of their own, so check only for a handler class that
    # the library defines.
    handlers = logging.getLogger("pycordis").handlers
    assert not [h for h in handlers if type(h).__module__.startswith("pycordis")]
    assert [r.msg for r in root.logger.buffer] == ["boom"]


@pytest.mark.upstream(
    "logger.spec.ts::Logger::logs an error whose errors array is empty"
)
@pytest.mark.upstream(
    "logger.spec.ts::Logger::flattens an aggregate error into its members"
)
def test_an_exception_logs_as_itself_and_a_group_logs_its_members(
    root: Context, captured: Capture
) -> None:
    plain = ValueError("plain")
    inner, other = ValueError("inner"), KeyError("other")
    root.logger.warning(plain)
    root.logger.warning(
        ExceptionGroup("outer", [inner, ExceptionGroup("nested", [other])])
    )

    assert [r.msg for r in captured.records] == [plain, inner, other]
    assert captured.records[0].exc_info is not None
    assert captured.records[0].exc_info[1] is plain


def test_a_fiber_error_reaches_the_exporter_with_its_fiber(
    root: Context, captured: Capture
) -> None:
    def broken(ctx: Context, config: object) -> None:
        raise RuntimeError("boom")

    fiber = root.use(broken)

    assert captured.names == ["broken"]
    assert captured.records[0].cordis_fiber is fiber  # type: ignore[attr-defined]


# computed with the upstream hash over the 256-color table
ROOT256, FOO256 = 41, 33


@pytest.mark.parametrize(
    ("name", "colors", "code"),
    [
        ("root", 1, 6),
        ("root", 3, ROOT256),
        ("foo:driver", 3, FOO256),
        ("foo:driver", 1, 3),
        ("custom", 1, 2),
        ("root", 0, None),
    ],
)
def test_the_color_of_a_name_is_stable(
    name: str, colors: int, code: int | None
) -> None:
    assert color_code(name, colors) == code


def test_colorize_wraps_a_value_in_the_escape_sequence() -> None:
    assert colorize("x", None) == "x"
    assert colorize("x", 2) == "\x1b[32mx\x1b[0m"
    assert colorize("x", 40) == "\x1b[38;5;40mx\x1b[0m"


def test_the_color_formatter_colors_the_name_and_leaves_the_rest() -> None:
    record = logging.LogRecord("pycordis.root", logging.INFO, "", 0, "hi", (), None)
    colored = ColorFormatter("%(colored_name)s %(message)s", colors=1).format(record)
    plain = ColorFormatter("%(colored_name)s %(message)s", colors=0).format(record)

    assert colored == f"\x1b[3{color_code('pycordis.root', 1)}mpycordis.root\x1b[0m hi"
    assert plain == "pycordis.root hi"


@pytest.mark.parametrize("method", ["debug", "info", "warning", "error"])
def test_a_level_method_of_the_service_logs_at_its_level(
    root: Context, captured: Capture, method: str
) -> None:
    getattr(root.logger, method)("value %s", 1)

    record = captured.records[0]
    assert record.levelname == method.upper()
    assert record.getMessage() == "value 1"
    assert captured.names == ["root"]


def test_the_extra_of_a_caller_joins_the_fiber_in_the_record(
    root: Context, captured: Capture
) -> None:
    root.logger.info("hi", extra={"request": 7})

    record = captured.records[0]
    assert record.request == 7  # type: ignore[attr-defined]
    assert record.cordis_fiber is root.fiber  # type: ignore[attr-defined]
