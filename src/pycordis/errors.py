"""Errors and call-site notes.

Start with `CordisError`, the exception that pycordis raises for a rule violation, and
`ValidationError`, which reports an invalid component config. `CallSite` records where
a call came from, so that an error raised later, from a scheduled task, can name the
code that started the work.
"""

import sys
from collections.abc import Iterable
from dataclasses import dataclass
from types import CodeType


class CordisError(Exception):
    """Raised when a caller breaks a pycordis rule.

    The message states the broken rule. Errors from invalid input use `TypeError` or
    `ValueError` instead.
    """


@dataclass(frozen=True, slots=True)
class Issue:
    """One reason that a config is invalid.

    Attributes:
        message: What is wrong.
        path: The names and indexes that lead to the invalid value. It is empty when the
            whole config is invalid.
    """

    message: str
    path: tuple[str | int, ...] = ()


class ValidationError(TypeError):
    """Raised when the config of a component does not pass the component's `Config`.

    The message lists each issue on its own line, with the path of the invalid value.

    Examples:
        ```python
        >>> error = ValidationError([Issue("expected int", ("server", "port"))])
        >>> print(error)
        invalid config:
          - expected int (at server.port)
        ```

    Attributes:
        issues: The reasons that the config is invalid.
    """

    def __init__(self, issues: Iterable[Issue]) -> None:
        self.issues = tuple(issues)
        lines = [
            f"  - {issue.message} (at {self.format_path(issue.path)})"
            if issue.path
            else f"  - {issue.message}"
            for issue in self.issues
        ]
        super().__init__("invalid config:\n" + "\n".join(lines))

    @staticmethod
    def format_path(path: Iterable[str | int]) -> str:
        """Return `path` as names and indexes that are joined by dots."""
        return ".".join(str(segment) for segment in path)


@dataclass(frozen=True, slots=True)
class CallSite:
    """The stack of a call, recorded at low cost and formatted on demand.

    A call site holds the code object and line number of each frame, not the frame
    itself, so it does not keep local variables alive. Use it to explain an error that
    happens after the call has returned.

    Examples:
        ```python
        >>> site = CallSite.capture()
        >>> try:
        ...     raise RuntimeError("later")
        ... except RuntimeError as error:
        ...     site.annotate(error, "Started by")
        ```

    Attributes:
        frames: The `(code, line number)` pairs of the stack, innermost first.
    """

    frames: tuple[tuple[CodeType, int], ...]

    @classmethod
    def capture(cls, skip: int = 0, limit: int = 16) -> "CallSite":
        """Record the stack of the caller.

        Args:
            skip: The number of innermost frames to leave out. With `0`, the first
                frame is the one that calls `capture`.
            limit: The maximum number of frames to record.

        Returns:
            The recorded call site.
        """
        frames: list[tuple[CodeType, int]] = []
        frame = sys._getframe(1 + skip)
        while frame is not None and len(frames) < limit:
            frames.append((frame.f_code, frame.f_lineno))
            frame = frame.f_back  # type: ignore[assignment]
        return cls(tuple(frames))

    def format(self) -> str:
        """Return the frames as text, outermost first, in the layout of a traceback."""
        return "\n".join(
            f'  File "{code.co_filename}", line {line}, in {code.co_qualname}'
            for code, line in reversed(self.frames)
        )

    def annotate(self, error: BaseException, label: str) -> None:
        """Add this call site to `error` as a note.

        Args:
            error: The exception to annotate.
            label: What the call site is, for example `"Started by"`.
        """
        error.add_note(f"{label}:\n{self.format()}")
