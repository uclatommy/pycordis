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
import importlib
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass

TRACEABILITY_MARKERS = ("upstream", "contract", "divergence")

CONTRACT_TABLE_HEADER = "| ID | Guarantee | Paper |"
DIVERGENCE_TABLE_HEADER = (
    "| ID | Kind | Upstream | pycordis | Contract | Reason and evidence |"
)

STATUSES = ("pending", "ported", "adapted", "diverged", "not-applicable")
ENTRY_FIELDS = {"status", "target", "hash", "mirror", "divergence", "note"}
DIVERGENCE_KINDS_BY_STATUS = {
    "adapted": {"adaptation"},
    "diverged": {"improvement", "correction", "omission"},
}
ISSUE_LINK = re.compile(r"https://github\.com/cordiverse/cordis/(?:issues|pull)/\d+")
README_REVISION = re.compile(
    r"Tracked upstream revision\*\*:\s*`[^`]+`\s*`([0-9a-f]{40})`"
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


def divergence_rows(markdown: str) -> list[dict[str, str]]:
    """Return the rows of the divergence table in ``docs/architecture.md``."""
    return parse_markdown_table(markdown, DIVERGENCE_TABLE_HEADER)


def divergence_kinds(markdown: str) -> dict[str, str]:
    """Return the ``Kind`` of each row of the divergence table, by row ID."""
    return {row["ID"]: row["Kind"] for row in divergence_rows(markdown)}


def readme_revision(readme: str) -> str:
    """Return the upstream revision that the README tracks."""
    match = README_REVISION.search(readme)
    if match is None:
        raise ValueError("README.md has no tracked upstream revision")
    return match.group(1)


def ledger_entries(ledger_text: str) -> dict[str, dict[str, object]]:
    """Return the ``[items."<ID>"]`` entries of the ledger, by inventory ID."""
    entries = tomllib.loads(ledger_text).get("items", {})
    if not isinstance(entries, dict) or not all(
        isinstance(entry, dict) for entry in entries.values()
    ):
        raise ValueError("the ledger [items] table holds one table for each ID")
    return entries


def check_revision(inventory_revision: str, readme: str) -> list[str]:
    """Fail when the inventory was generated from another revision than the README."""
    tracked = readme_revision(readme)
    if inventory_revision == tracked:
        return []
    return [
        f"conformance/upstream.json is from revision {inventory_revision}, but "
        f"README.md tracks {tracked}: regenerate the inventory with "
        "scripts/upstream_inventory.py from a clone at the tracked revision"
    ]


def check_inventory_coverage(
    items: Mapping[str, Mapping[str, object]],
    entries: Mapping[str, Mapping[str, object]],
) -> list[str]:
    """Fail on an inventory item without a ledger entry, and the reverse."""
    failures = [
        f"inventory item {item_id}: no ledger entry; add one with "
        "scripts/upstream_inventory.py --ledger-stubs"
        for item_id in sorted(set(items) - set(entries))
    ]
    failures += [
        f"ledger entry {item_id}: no inventory item; delete the entry"
        for item_id in sorted(set(entries) - set(items))
    ]
    return failures


def check_hash_pinning(
    items: Mapping[str, Mapping[str, object]],
    entries: Mapping[str, Mapping[str, object]],
) -> list[str]:
    """Fail when a reviewed item's upstream hash differs from its ledger hash."""
    return [
        f"ledger entry {item_id}: review the upstream change to `{item_id}`, "
        f"then copy hash `{items[item_id]['hash']}` into its ledger entry"
        for item_id, entry in sorted(entries.items())
        if entry.get("status") in STATUSES[1:]
        and item_id in items
        and isinstance(entry.get("hash"), str)
        and entry["hash"] != items[item_id]["hash"]
    ]


def _entry_failures(
    entry: Mapping[str, object],
    kind: object,
    divergences: Mapping[str, str],
) -> list[str]:
    """Return what is wrong in one ledger entry, without the entry's ID."""
    status = entry.get("status")
    if status not in STATUSES:
        return [f"status {status!r} is not one of {', '.join(STATUSES)}"]
    failures = [
        f"field {name!r} is not a ledger field"
        for name in sorted(set(entry) - ENTRY_FIELDS)
    ]
    has_hash = "hash" in entry
    if status == "pending" and has_hash:
        failures.append("a pending entry has no hash: a hash is copied after review")
    if status != "pending" and not isinstance(entry.get("hash"), str):
        failures.append(f"a {status} entry needs the upstream hash of its review")
    if status == "not-applicable" and not entry.get("note"):
        failures.append("a not-applicable entry needs a note that gives the reason")
    if not isinstance(entry.get("mirror", False), bool):
        failures.append("mirror must be true or false")
    if "target" in entry and kind not in (None, "symbol", "member"):
        failures.append(f"a {kind} item has no target; its upstream marker is the link")
    if status == "pending" and "target" in entry:
        failures.append("a pending entry names a target: set its reviewed status")
    return failures + _divergence_failures(entry, status, divergences)


def _divergence_failures(
    entry: Mapping[str, object], status: str, divergences: Mapping[str, str]
) -> list[str]:
    divergence = entry.get("divergence")
    kinds = DIVERGENCE_KINDS_BY_STATUS.get(status)
    if kinds is None:
        return [] if divergence is None else [f"a {status} entry has no divergence"]
    if divergence is None:
        return [f"a {status} entry needs a divergence ID of kind {sorted(kinds)}"]
    if divergence not in divergences:
        return [f"divergence {divergence!r} is not a row of docs/architecture.md"]
    if divergences[divergence] not in kinds:
        return [
            f"divergence {divergence} has kind {divergences[divergence]}, but a "
            f"{status} entry needs kind {sorted(kinds)}"
        ]
    return []


def check_status_consistency(
    items: Mapping[str, Mapping[str, object]],
    entries: Mapping[str, Mapping[str, object]],
    divergences: Mapping[str, str],
    occurrences: list[MarkerOccurrence],
) -> list[str]:
    """Fail when an entry's fields do not agree with its status.

    Args:
        items: The inventory items, by ID.
        entries: The ledger entries, by ID.
        divergences: The kind of each divergence row, by ID.
        occurrences: The traceability markers of the test suite.
    """
    failures = [
        f"ledger entry {item_id}: {problem}"
        for item_id, entry in sorted(entries.items())
        for problem in _entry_failures(
            entry, items.get(item_id, {}).get("kind"), divergences
        )
    ]
    failures += [
        f"{occurrence.location}: @pytest.mark.upstream names {occurrence.id!r}, "
        "whose ledger status is pending; set the status after review"
        for occurrence in occurrences
        if occurrence.marker == "upstream"
        and occurrence.id is not None
        and entries.get(occurrence.id, {}).get("status") == "pending"
    ]
    return failures


def resolves(target: str) -> bool:
    """Return ``True`` when the dotted name imports and its attributes exist."""
    parts = target.split(".")
    for split in range(len(parts), 0, -1):
        module_name = ".".join(parts[:split])
        try:
            value: object = importlib.import_module(module_name)
        except ImportError:
            continue
        for name in parts[split:]:
            if not hasattr(value, name):
                return False
            value = getattr(value, name)
        return True
    return False


def check_target_resolution(entries: Mapping[str, Mapping[str, object]]) -> list[str]:
    """Fail when an entry's ``target`` does not import."""
    return [
        f"ledger entry {item_id}: target {entry['target']!r} does not resolve; "
        "fix the name or implement the symbol"
        for item_id, entry in sorted(entries.items())
        if "target" in entry
        and not (isinstance(entry["target"], str) and resolves(entry["target"]))
    ]


def check_upstream_markers(
    items: Mapping[str, Mapping[str, object]],
    entries: Mapping[str, Mapping[str, object]],
    occurrences: list[MarkerOccurrence],
) -> list[str]:
    """Fail on an unknown ``upstream`` ID, and on a reviewed case with no test.

    A ``ported`` or ``adapted`` case needs a running test with its ``upstream``
    marker.
    """
    upstream = [o for o in occurrences if o.marker == "upstream" and o.id is not None]
    failures = [
        f"{o.location}: @pytest.mark.upstream names {o.id!r}, which is not a case "
        "of conformance/upstream.json"
        for o in upstream
        if items.get(o.id or "", {}).get("kind") != "case"
    ]
    running = {o.id for o in upstream if o.running}
    failures += [
        f"ledger entry {item_id}: status {entry['status']} needs a running test "
        "marked upstream with this ID"
        for item_id, entry in sorted(entries.items())
        if entry.get("status") in ("ported", "adapted")
        and items.get(item_id, {}).get("kind") == "case"
        and item_id not in running
    ]
    return failures


@dataclass(frozen=True)
class SourceVocabulary:
    """The strings and state names that the package source contains.

    Attributes:
        strings: Every string literal, and every f-string with each replacement
            field written as ``{}``.
        enum_members: The member names of every ``Enum`` subclass.
    """

    strings: frozenset[str]
    enum_members: frozenset[str]


def _is_enum_base(base: ast.expr) -> bool:
    if isinstance(base, ast.Attribute):
        return base.attr.endswith("Enum")
    return isinstance(base, ast.Name) and base.id.endswith("Enum")


def source_vocabulary(sources: Mapping[str, str]) -> SourceVocabulary:
    """Read the string literals and enum members from the ``ast`` of ``sources``."""
    strings: set[str] = set()
    members: set[str] = set()
    for filename, text in sources.items():
        tree = ast.parse(text, filename=filename)
        fragments: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                parts = []
                for value in node.values:
                    if isinstance(value, ast.Constant):
                        fragments.add(id(value))
                    parts.append(
                        str(value.value) if isinstance(value, ast.Constant) else "{}"
                    )
                strings.add("".join(parts))
            elif isinstance(node, ast.ClassDef) and any(
                _is_enum_base(base) for base in node.bases
            ):
                for statement in node.body:
                    if isinstance(statement, ast.Assign):
                        members.update(
                            target.id
                            for target in statement.targets
                            if isinstance(target, ast.Name)
                        )
        strings.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in fragments
        )
    return SourceVocabulary(frozenset(strings), frozenset(members))


def check_vocabulary(
    items: Mapping[str, Mapping[str, object]],
    entries: Mapping[str, Mapping[str, object]],
    vocabulary: SourceVocabulary,
) -> list[str]:
    """Fail when a reviewed message, event, or state is absent from the source.

    A ``pending`` entry is not checked, and an entry with a ``divergence`` states
    why the name differs.
    """
    failures = []
    for item_id, entry in sorted(entries.items()):
        item = items.get(item_id, {})
        kind, text = item.get("kind"), item.get("text")
        if (
            kind not in ("message", "event", "state")
            or not isinstance(text, str)
            or entry.get("status") == "pending"
            or "divergence" in entry
        ):
            continue
        present = text in (
            vocabulary.enum_members if kind == "state" else vocabulary.strings
        )
        if not present:
            failures.append(
                f"ledger entry {item_id}: the {kind} {text!r} does not occur in "
                "src/pycordis; use the upstream text or add a divergence"
            )
    return failures


def _row_evidence_failures(
    row: Mapping[str, str],
    contract_ids: list[str],
    adapted: bool,
    running_divergence: bool,
) -> list[str]:
    """Return what the evidence of one divergence row lacks."""
    kind = row["Kind"]
    if kind == "adaptation":
        return [] if adapted else ["no adapted ledger entry names this row"]
    if kind == "omission":
        return [] if row["Reason and evidence"] else ["the Reason cell is empty"]
    if kind not in ("improvement", "correction"):
        return [f"kind {kind!r} is not adaptation, improvement, correction or omission"]
    failures = []
    named = [cell for cell in re.split(r"[,\s]+", row["Contract"]) if cell]
    unknown = [name for name in named if name not in contract_ids]
    if not named or unknown:
        failures.append(
            "the Contract cell must name known contract IDs"
            + (f", not {unknown}" if unknown else "")
        )
    if not running_divergence:
        failures.append("no running test is marked divergence with this ID")
    if kind == "correction" and not ISSUE_LINK.search(row["Reason and evidence"]):
        failures.append(
            "the Reason cell needs a link to an issue or pull request in "
            "github.com/cordiverse/cordis"
        )
    return failures


def check_divergence_evidence(
    rows: list[dict[str, str]],
    contract_ids: list[str],
    entries: Mapping[str, Mapping[str, object]],
    occurrences: list[MarkerOccurrence],
) -> list[str]:
    """Fail when a divergence row lacks the evidence its kind requires.

    Also fails for a row that no ledger entry and no ``divergence`` marker names.
    The running ``upstream`` test of each adapted case is checked with the other
    marker rules.
    """
    by_status: dict[str, set[object]] = {"adapted": set(), "any": set()}
    for entry in entries.values():
        by_status["any"].add(entry.get("divergence"))
        if entry.get("status") == "adapted":
            by_status["adapted"].add(entry.get("divergence"))
    marked = {o.id for o in occurrences if o.marker == "divergence"}
    running = {o.id for o in occurrences if o.marker == "divergence" and o.running}
    failures = []
    for row in rows:
        row_id = row["ID"]
        failures += [
            f"divergence {row_id}: {problem}"
            for problem in _row_evidence_failures(
                row,
                contract_ids,
                row_id in by_status["adapted"],
                row_id in running,
            )
        ]
        if row_id not in by_status["any"] and row_id not in marked:
            failures.append(
                f"divergence {row_id}: no ledger entry and no marker names this "
                "row; delete it"
            )
    return failures
