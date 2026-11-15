"""Static conformance checks over the conformance data.

Each check is a pure function that takes parsed data and returns a list of failure
messages; an empty list means the data passes. Every message names the item and the
action a maintainer must take.

The traceability markers ``upstream``, ``contract`` and ``divergence`` are the link
between a test and the conformance data. ``scan_markers`` reads them from the ``ast``
of the test files, so a check gives the same result as test collection and does not
depend on which tests were selected.
"""

import ast
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass

TRACEABILITY_MARKERS = ("upstream", "contract", "divergence")

CONTRACT_TABLE_HEADER = "| ID | Guarantee | Paper |"
DIVERGENCE_TABLE_HEADER = (
    "| ID | Kind | Upstream | pycordis | Contract | Reason and evidence |"
)


def parse_markdown_table(markdown: str, header: str) -> list[dict[str, str]]:
    """Return the data rows of the table whose header row is exactly ``header``.

    Cells are trimmed and stripped of surrounding backticks. Raises ``ValueError``
    when the table has no such header row.
    """
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != header:
            continue
        columns = [cell.strip() for cell in header.strip().strip("|").split("|")]
        rows: list[dict[str, str]] = []
        for row_line in lines[index + 1 :]:
            stripped = row_line.strip()
            if not stripped.startswith("|"):
                break
            if set(stripped) <= set("|-: "):
                continue  # separator row
            cells = [cell.strip().strip("`") for cell in stripped.strip("|").split("|")]
            if len(cells) != len(columns):
                raise ValueError(f"{header}: a row does not match the columns")
            rows.append(dict(zip(columns, cells, strict=True)))
        return rows
    raise ValueError(f"no table with header {header!r}")


def table_section_ids(markdown: str, header: str) -> list[str]:
    """Return the ``ID`` column of the table whose header row is exactly ``header``."""
    return [row["ID"] for row in parse_markdown_table(markdown, header)]


def pending_contract_ids(ledger_text: str) -> list[str]:
    """Return the IDs of the ``pending`` list of the ledger's ``[contract]`` table."""
    try:
        pending = tomllib.loads(ledger_text)["contract"]["pending"]
    except (KeyError, TypeError) as error:
        raise ValueError("the ledger has no [contract] pending list") from error
    if not isinstance(pending, list):
        raise ValueError("the [contract] pending entry is not a list")
    return list(pending)


@dataclass(frozen=True)
class MarkerOccurrence:
    """One traceability marker written on a test.

    Attributes:
        marker: The marker name: ``upstream``, ``contract`` or ``divergence``.
        id: The ID argument as a string literal, or ``None`` when an argument is
            not a string literal.
        location: The file and qualified name, e.g. ``tests/test_x.py::test_case``.
        running: ``True`` when the test runs: no ``skip`` mark and no ``skipif``
            mark with a literal ``True`` condition, on the test, an enclosing
            class, or the ``pytest.param`` that carries the marker. A skipped
            ancestor makes every marker below it not run. An ``xfail`` mark
            still runs: it states an expected result, so it tests the rule.
    """

    marker: str
    id: str | None
    location: str
    running: bool


def _pytest_mark(node: ast.expr) -> tuple[str, list[ast.expr]] | None:
    """Return ``(mark name, call arguments)`` for a ``pytest.mark.<name>`` node."""
    target = node.func if isinstance(node, ast.Call) else node
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "mark"
        and isinstance(target.value.value, ast.Name)
        and target.value.value.id == "pytest"
    ):
        arguments = node.args if isinstance(node, ast.Call) else []
        return target.attr, list(arguments)
    return None


def _ids(arguments: list[ast.expr]) -> list[str | None]:
    """Return one entry per marker argument: the literal ID, or ``None``."""
    if not arguments:
        return [None]
    return [
        argument.value
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        else None
        for argument in arguments
    ]


def _skips(name: str, arguments: list[ast.expr]) -> bool:
    """Return ``True`` for a ``skip`` mark or a ``skipif`` of a literal ``True``."""
    if name == "skip":
        return True
    return (
        name == "skipif"
        and bool(arguments)
        and isinstance(arguments[0], ast.Constant)
        and arguments[0].value is True
    )


def _decorator_marks(decorators: list[ast.expr]) -> list[tuple[str, str | None]]:
    """Return the traceability ``(marker, id)`` pairs among a node's decorators."""
    marks: list[tuple[str, str | None]] = []
    for decorator in decorators:
        mark = _pytest_mark(decorator)
        if mark is None or mark[0] not in TRACEABILITY_MARKERS:
            continue
        marks.extend((mark[0], literal_id) for literal_id in _ids(mark[1]))
    return marks


def _decorator_is_skipped(decorators: list[ast.expr]) -> bool:
    marks = (mark for mark in map(_pytest_mark, decorators) if mark is not None)
    return any(_skips(name, arguments) for name, arguments in marks)


def _parametrize_marks(
    decorators: list[ast.expr],
) -> list[tuple[str, str | None, bool]]:
    """Return ``(marker, id, runs)`` for traceability marks on ``pytest.param`` sets.

    Only ``pytest.mark.parametrize("...", [pytest.param(..., marks=...)])`` with a
    literal list of parameters is read.
    """
    marks: list[tuple[str, str | None, bool]] = []
    for decorator in decorators:
        mark = _pytest_mark(decorator)
        if mark is None or mark[0] != "parametrize" or len(mark[1]) < 2:
            continue
        values = mark[1][1]
        if not isinstance(values, (ast.List, ast.Tuple)):
            continue
        for param in values.elts:
            if not (
                isinstance(param, ast.Call)
                and isinstance(param.func, ast.Attribute)
                and param.func.attr == "param"
                and isinstance(param.func.value, ast.Name)
                and param.func.value.id == "pytest"
            ):
                continue
            marks_keyword = next(
                (kw for kw in param.keywords if kw.arg == "marks"), None
            )
            if marks_keyword is None:
                continue
            items = marks_keyword.value
            item_nodes = (
                list(items.elts)
                if isinstance(items, (ast.List, ast.Tuple))
                else [items]
            )
            param_marks = [m for m in map(_pytest_mark, item_nodes) if m is not None]
            skipped = any(_skips(name, args) for name, args in param_marks)
            for name, arguments in param_marks:
                if name in TRACEABILITY_MARKERS:
                    marks.extend(
                        (name, literal_id, not skipped)
                        for literal_id in _ids(arguments)
                    )
    return marks


class _MarkerVisitor(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.occurrences: list[MarkerOccurrence] = []
        self._qualname: list[str] = []
        self._ancestor_running = True

    def _visit_definition(self, node: ast.ClassDef | ast.FunctionDef) -> None:
        running = self._ancestor_running and not _decorator_is_skipped(
            node.decorator_list
        )
        location = "::".join([self.filename, *self._qualname, node.name])
        for marker, literal_id in _decorator_marks(node.decorator_list):
            self.occurrences.append(
                MarkerOccurrence(marker, literal_id, location, running)
            )
        for marker, literal_id, param_runs in _parametrize_marks(node.decorator_list):
            self.occurrences.append(
                MarkerOccurrence(marker, literal_id, location, running and param_runs)
            )
        saved = self._ancestor_running
        self._ancestor_running = running
        self._qualname.append(node.name)
        self.generic_visit(node)
        self._qualname.pop()
        self._ancestor_running = saved

    visit_ClassDef = _visit_definition
    visit_FunctionDef = _visit_definition
    visit_AsyncFunctionDef = _visit_definition


def scan_markers(sources: Mapping[str, str]) -> list[MarkerOccurrence]:
    """Return every traceability marker written in ``sources``.

    Args:
        sources: A mapping of display file name to Python source, typically every
            test file of the suite.

    Returns:
        One ``MarkerOccurrence`` per marker argument, in file and source order.
    """
    occurrences: list[MarkerOccurrence] = []
    for filename in sorted(sources):
        visitor = _MarkerVisitor(filename)
        visitor.visit(ast.parse(sources[filename], filename=filename))
        occurrences.extend(visitor.occurrences)
    return occurrences


def check_marker_arguments(occurrences: list[MarkerOccurrence]) -> list[str]:
    """Fail when a marker has an ID argument that is not a string literal."""
    return [
        f"{occurrence.location}: @pytest.mark.{occurrence.marker} needs a "
        "string-literal ID argument"
        for occurrence in occurrences
        if occurrence.id is None
    ]


def check_marker_ids(
    occurrences: list[MarkerOccurrence],
    contract_ids: list[str],
    divergence_ids: list[str],
) -> list[str]:
    """Fail when a ``contract`` or ``divergence`` marker names an unknown ID."""
    known = {"contract": set(contract_ids), "divergence": set(divergence_ids)}
    failures = []
    for occurrence in occurrences:
        ids = known.get(occurrence.marker)
        if ids is not None and occurrence.id is not None and occurrence.id not in ids:
            failures.append(
                f"{occurrence.location}: @pytest.mark.{occurrence.marker} names "
                f"{occurrence.id!r}, which is not a row of docs/architecture.md"
            )
    return failures


def check_contract_coverage(
    contract_ids: list[str],
    pending: list[str],
    occurrences: list[MarkerOccurrence],
) -> list[str]:
    """Fail when a contract ID has no running ``contract`` test and is not pending.

    Also fails when ``[contract] pending`` names an ID that is not in the contract
    table (an entry for an ID that no longer exists is stale too) or that already
    has a running test — a pending entry is removed once its test runs.
    """
    running = {
        occurrence.id
        for occurrence in occurrences
        if occurrence.marker == "contract" and occurrence.running
    }
    pending_set = set(pending)
    failures = [
        f"contract {contract_id}: no running test marked contract and not in "
        "[contract] pending"
        for contract_id in contract_ids
        if contract_id not in running and contract_id not in pending_set
    ]
    failures += [
        f"[contract] pending names {contract_id!r}, which is not an ID of the "
        "contract table in docs/architecture.md"
        for contract_id in pending
        if contract_id not in set(contract_ids)
    ]
    failures += [
        f"[contract] pending is stale: {contract_id} has a running test marked "
        "contract; delete the pending entry"
        for contract_id in pending
        if contract_id in running
    ]
    return failures
