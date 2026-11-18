"""Tests for the static conformance checks.

Each check must pass on the committed conformance data and must fail with a message
naming the item on a minimal bad input.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conformance_checks import (
    CONTRACT_TABLE_HEADER,
    DIVERGENCE_TABLE_HEADER,
    MarkerOccurrence,
    check_contract_coverage,
    check_divergence_evidence,
    check_hash_pinning,
    check_inventory_coverage,
    check_marker_arguments,
    check_marker_ids,
    check_revision,
    check_status_consistency,
    check_target_resolution,
    check_upstream_markers,
    check_vocabulary,
    divergence_kinds,
    divergence_rows,
    ledger_entries,
    pending_contract_ids,
    readme_revision,
    resolves,
    scan_markers,
    source_vocabulary,
    table_section_ids,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_MD = (REPO_ROOT / "docs" / "architecture.md").read_text("utf-8")
LEDGER_TOML = (REPO_ROOT / "conformance" / "ledger.toml").read_text("utf-8")
README_MD = (REPO_ROOT / "README.md").read_text("utf-8")
INVENTORY = json.loads((REPO_ROOT / "conformance" / "upstream.json").read_text("utf-8"))

CASE = "fiber.spec.ts::Fiber::unloads"
SYMBOL = "src/fiber.ts:Fiber"
ITEMS = {
    CASE: {"kind": "case", "hash": "h1"},
    SYMBOL: {"kind": "symbol", "hash": "h2"},
}
VOCABULARY = source_vocabulary(
    {
        "m.py": (
            "import enum\nclass S(enum.Enum):\n    ACTIVE = 1\n"
            "x = 'a'\ny = f'b {x} c'\n"
            "from enum import StrEnum\nclass T(StrEnum):\n    ONE = 'x'\n"
            "class P:\n    NOT_STATE = 1\n"
        )
    }
)
LINK = "https://github.com/cordiverse/cordis/issues/1"
DIVERGENCES = {"ADAPT-1": "adaptation", "OMIT-1": "omission"}


def _suite_sources() -> dict[str, str]:
    """Read every test file, as the static marker scan of the suite does."""
    tests_dir = REPO_ROOT / "tests"
    return {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text("utf-8")
        for path in sorted(tests_dir.rglob("*.py"))
    }


def _src_sources() -> dict[str, str]:
    """Read every module of the package."""
    return {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text("utf-8")
        for path in sorted((REPO_ROOT / "src" / "pycordis").rglob("*.py"))
    }


def _occurrences(
    marker: str = "contract",
    id: str | None = "EFF-REVERT",
    running: bool = True,
) -> list[MarkerOccurrence]:
    return [MarkerOccurrence(marker, id, "tests/test_sample.py::test_case", running)]


_CONTRACT_TABLE = (
    "| ID | Guarantee | Paper |\n|---|---|---|\n| `A-1` | Does a. | Thm 1 |\n"
)
_DIVERGENCE_TABLE = (
    "| ID | Kind | Upstream | pycordis | Contract | Reason and evidence |\n"
    "|---|---|---|---|---|---|\n| `DIV-1` | omission | a | none |  | Not portable. |\n"
)


def _entry(**fields: object) -> dict[str, dict[str, object]]:
    return {SYMBOL: {"status": "ported", "hash": "h2", **fields}}


def _upstream_marker(id: str = CASE, running: bool = True) -> list[MarkerOccurrence]:
    return _occurrences("upstream", id, running)


def _status(
    entries: dict[str, dict[str, object]],
    occurrences: list[MarkerOccurrence] | None = None,
) -> list[str]:
    return check_status_consistency(ITEMS, entries, DIVERGENCES, occurrences or [])


def _row(kind: str, contract: str = "", reason: str = "") -> list[dict[str, str]]:
    row = {"ID": "D-1", "Kind": kind, "Contract": contract}
    return [{**row, "Reason and evidence": reason}]


def _evidence(
    rows: list[dict[str, str]],
    entries: dict[str, dict[str, object]] | None = None,
    divergence_marker: bool = True,
) -> list[str]:
    marks = _occurrences("divergence", "D-1") if divergence_marker else []
    return check_divergence_evidence(rows, ["EFF-REVERT"], entries or {}, marks)


def _vocabulary(kind: str, text: str, **entry: object) -> list[str]:
    items = {"x": {"kind": kind, "text": text}}
    entries = {"x": {"status": "ported", "hash": "h", **entry}}
    return check_vocabulary(items, entries, VOCABULARY)


def _committed_occurrences() -> list[MarkerOccurrence]:
    return scan_markers(_suite_sources())


def _bad_cases() -> list[tuple[str, Callable[[], list[str]], str]]:
    occ = _occurrences
    return [
        (
            "marker argument is not a string literal",
            lambda: check_marker_arguments(occ(id=None)),
            "needs a string-literal ID argument",
        ),
        (
            "contract marker names an unknown ID",
            lambda: check_marker_ids(occ(id="NO-SUCH"), ["EFF-REVERT"], []),
            "is not a row of docs/architecture.md",
        ),
        (
            "divergence marker names an unknown ID",
            lambda: check_marker_ids(occ("divergence", "NO-SUCH"), [], ["DIV-1"]),
            "is not a row of docs/architecture.md",
        ),
        (
            "contract ID has no running test and is not pending",
            lambda: check_contract_coverage(["EFF-REVERT"], [], occ(running=False)),
            "EFF-REVERT: no running test",
        ),
        (
            "pending names an ID outside the contract table",
            lambda: check_contract_coverage(["EFF-REVERT"], ["GONE-ID"], []),
            "GONE-ID",
        ),
        (
            "pending entry is stale",
            lambda: check_contract_coverage(["EFF-REVERT"], ["EFF-REVERT"], occ()),
            "pending is stale: EFF-REVERT",
        ),
        (
            "inventory revision differs from the README",
            lambda: check_revision("0" * 40, README_MD),
            "README.md tracks",
        ),
        (
            "inventory item has no ledger entry",
            lambda: check_inventory_coverage(ITEMS, {}),
            f"inventory item {CASE}: no ledger entry",
        ),
        (
            "ledger entry has no inventory item",
            lambda: check_inventory_coverage({}, {"gone": {"status": "pending"}}),
            "ledger entry gone: no inventory item",
        ),
        (
            "reviewed hash is stale",
            lambda: check_hash_pinning(ITEMS, _entry(hash="old")),
            f"review the upstream change to `{SYMBOL}`, then copy hash `h2`",
        ),
        (
            "pending entry has a hash",
            lambda: _status({SYMBOL: {"status": "pending", "hash": "h2"}}),
            "a pending entry has no hash",
        ),
        (
            "reviewed entry has no hash",
            lambda: _status({SYMBOL: {"status": "ported"}}),
            "needs the upstream hash",
        ),
        (
            "not-applicable entry has no note",
            lambda: _status(_entry(status="not-applicable")),
            "needs a note",
        ),
        (
            "status is unknown",
            lambda: _status(_entry(status="done")),
            "'done' is not one of",
        ),
        (
            "field is misspelled",
            lambda: _status(_entry(targt="pycordis")),
            "'targt' is not a ledger field",
        ),
        (
            "adapted entry has no divergence",
            lambda: _status(_entry(status="adapted")),
            "needs a divergence ID",
        ),
        (
            "adapted entry names a divergence of another kind",
            lambda: _status(_entry(status="adapted", divergence="OMIT-1")),
            "has kind omission",
        ),
        (
            "diverged entry names an unknown divergence",
            lambda: _status(_entry(status="diverged", divergence="NO-ROW")),
            "'NO-ROW' is not a row",
        ),
        (
            "ported entry names a divergence",
            lambda: _status(_entry(divergence="ADAPT-1")),
            "has no divergence",
        ),
        (
            "case entry has a target",
            lambda: _status({CASE: {"status": "ported", "hash": "h1", "target": "x"}}),
            "a case item has no target",
        ),
        (
            "pending symbol entry has a target",
            lambda: _status({SYMBOL: {"status": "pending", "target": "pycordis"}}),
            "a pending entry names a target",
        ),
        (
            "upstream marker names a pending case",
            lambda: _status({CASE: {"status": "pending"}}, _upstream_marker()),
            "whose ledger status is pending",
        ),
        (
            "target does not resolve",
            lambda: check_target_resolution(_entry(target="pycordis.no_such_name")),
            "'pycordis.no_such_name' does not resolve",
        ),
        (
            "upstream marker names an unknown ID",
            lambda: check_upstream_markers(ITEMS, {}, _upstream_marker("no::such")),
            "is not a case of conformance/upstream.json",
        ),
        (
            "upstream marker names a non-case item",
            lambda: check_upstream_markers(ITEMS, {}, _upstream_marker(SYMBOL)),
            "is not a case",
        ),
        (
            "ported case has no running test",
            lambda: check_upstream_markers(
                ITEMS,
                {CASE: {"status": "ported", "hash": "h1"}},
                _upstream_marker(running=False),
            ),
            "needs a running test marked upstream",
        ),
        (
            "message is absent from the source",
            lambda: _vocabulary("message", "gone {}"),
            "the message 'gone {}' does not occur",
        ),
        (
            "event is absent from the source",
            lambda: _vocabulary("event", "internal/gone"),
            "the event 'internal/gone'",
        ),
        (
            "state is not an enum member",
            lambda: _vocabulary("state", "a"),
            "the state 'a'",
        ),
        (
            "adaptation row is used by no adapted entry",
            lambda: _evidence(_row("adaptation"), {"x": {"divergence": "D-1"}}),
            "no adapted ledger entry names this row",
        ),
        (
            "omission row has no reason",
            lambda: _evidence(_row("omission")),
            "the Reason cell is empty",
        ),
        (
            "improvement names an unknown contract ID",
            lambda: _evidence(_row("improvement", "NO-ID")),
            "known contract IDs, not ['NO-ID']",
        ),
        (
            "improvement names no contract ID",
            lambda: _evidence(_row("improvement")),
            "must name known contract IDs",
        ),
        (
            "improvement has no running divergence test",
            lambda: _evidence(_row("improvement", "EFF-REVERT"), {}, False),
            "no running test is marked divergence",
        ),
        (
            "improvement test is skipped",
            lambda: check_divergence_evidence(
                _row("improvement", "EFF-REVERT"),
                ["EFF-REVERT"],
                {},
                _occurrences("divergence", "D-1", running=False),
            ),
            "no running test is marked divergence",
        ),
        (
            "improvement names one unknown contract ID among known ones",
            # the table parser strips the backticks at the two ends of a cell only
            lambda: _evidence(_row("improvement", "EFF-REVERT`, `NO-ID")),
            "not ['NO-ID']",
        ),
        (
            "correction has no link",
            lambda: _evidence(_row("correction", "EFF-REVERT", "Upstream bug.")),
            "needs a link to an issue or pull request",
        ),
        (
            "row is used by nothing",
            lambda: _evidence(_row("omission", "", "Not portable."), {}, False),
            "no ledger entry and no marker names this row",
        ),
        (
            "row has an unknown kind",
            lambda: _evidence(_row("change")),
            "kind 'change' is not",
        ),
    ]


@pytest.mark.parametrize(
    ("name", "check", "expected"),
    _bad_cases(),
    ids=[name for name, _, _ in _bad_cases()],
)
def test_check_fails_on_bad_data(
    name: str, check: Callable[[], list[str]], expected: str
) -> None:
    failures = check()
    assert failures, name
    assert any(expected in failure for failure in failures), failures


@pytest.mark.slow
def test_checks_pass_on_committed_data() -> None:
    contract_ids = table_section_ids(ARCHITECTURE_MD, CONTRACT_TABLE_HEADER)
    divergence_ids = table_section_ids(ARCHITECTURE_MD, DIVERGENCE_TABLE_HEADER)
    occurrences = _committed_occurrences()
    pending = pending_contract_ids(LEDGER_TOML)
    assert check_marker_arguments(occurrences) == []
    assert check_marker_ids(occurrences, contract_ids, divergence_ids) == []
    assert check_contract_coverage(contract_ids, pending, occurrences) == []
    items = INVENTORY["items"]
    entries = ledger_entries(LEDGER_TOML)
    divergences = divergence_kinds(ARCHITECTURE_MD)
    assert check_revision(INVENTORY["revision"], README_MD) == []
    assert check_inventory_coverage(items, entries) == []
    assert check_hash_pinning(items, entries) == []
    assert check_status_consistency(items, entries, divergences, occurrences) == []
    assert check_target_resolution(entries) == []
    assert check_upstream_markers(items, entries, occurrences) == []
    assert check_vocabulary(items, entries, source_vocabulary(_src_sources())) == []
    rows = divergence_rows(ARCHITECTURE_MD)
    assert check_divergence_evidence(rows, contract_ids, entries, occurrences) == []


def test_contract_marker_on_a_running_test_counts_as_coverage() -> None:
    occurrences = scan_markers(
        {
            "tests/test_a.py": (
                "import pytest\n"
                "@pytest.mark.contract('EFF-REVERT')\n"
                "def test_revert():\n"
                "    pass\n"
            )
        }
    )
    assert [(o.marker, o.id, o.running) for o in occurrences] == [
        ("contract", "EFF-REVERT", True)
    ]
    assert check_contract_coverage(["EFF-REVERT"], [], occurrences) == []


def test_skipped_test_does_not_count_as_coverage() -> None:
    occurrences = scan_markers(
        {
            "tests/test_a.py": (
                "import pytest\n"
                "\n"
                "@pytest.mark.contract('EFF-REVERT')\n"
                "@pytest.mark.skip(reason='not ported')\n"
                "def test_skipped():\n"
                "    pass\n"
                "\n"
                "@pytest.mark.contract('EFF-ISOLATE')\n"
                "@pytest.mark.skipif(True, reason='not ported')\n"
                "async def test_skipped_async():\n"
                "    pass\n"
                "\n"
                "@pytest.mark.contract('LIFE-ORDER')\n"
                "@pytest.mark.skipif(sys.version_info > (99,), reason='later')\n"
                "def test_conditional():\n"
                "    pass\n"
                "\n"
                "@pytest.mark.contract('LIFE-FAILURE')\n"
                "@pytest.mark.xfail(reason='expected to fail')\n"
                "def test_expected_failure():\n"
                "    pass\n"
            )
        }
    )
    running = {o.id for o in occurrences if o.running}
    assert running == {"LIFE-ORDER", "LIFE-FAILURE"}


def test_divergence_rules_follow_the_same_decision() -> None:
    occurrences = scan_markers(
        {
            "tests/test_a.py": (
                "import pytest\n"
                "\n"
                "@pytest.mark.divergence('DIV-1')\n"
                "@pytest.mark.xfail(reason='negative test of the specification')\n"
                "def test_negative():\n"
                "    pass\n"
                "\n"
                "@pytest.mark.divergence(f'DIV-{2}')\n"
                "def test_dynamic_id():\n"
                "    pass\n"
            )
        }
    )
    assert check_marker_arguments(occurrences) == [
        "tests/test_a.py::test_dynamic_id: @pytest.mark.divergence needs a "
        "string-literal ID argument"
    ]
    # the xfail reason is not reported, and the valid literal ID names a known
    # row, so the ID check passes; only a literal ID outside the table fails:
    assert check_marker_ids(occurrences, [], ["DIV-1"]) == []
    assert [(o.id, o.running) for o in occurrences] == [
        ("DIV-1", True),
        (None, True),
    ]


def test_class_marks_and_param_marks_are_scanned() -> None:
    occurrences = scan_markers(
        {
            "tests/test_a.py": (
                "import pytest\n"
                "\n"
                "@pytest.mark.skip(reason='not ported')\n"
                "class TestSkipped:\n"
                "    @pytest.mark.contract('EFF-REVERT')\n"
                "    def test_inherited_skip():\n"
                "        pass\n"
                "\n"
                "class TestLive:\n"
                "    @pytest.mark.parametrize(\n"
                "        'value',\n"
                "        [\n"
                "            pytest.param(\n"
                "                1, marks=pytest.mark.contract('EFF-ISOLATE')\n"
                "            ),\n"
                "            pytest.param(\n"
                "                2,\n"
                "                marks=[\n"
                "                    pytest.mark.contract('LIFE-ORDER'),\n"
                "                    pytest.mark.skip(reason='not ported'),\n"
                "                ],\n"
                "            ),\n"
                "            pytest.param(\n"
                "                3,\n"
                "                marks=[\n"
                "                    pytest.mark.contract('EFF-ISOLATE'),\n"
                "                    pytest.mark.contract('REG-WELLFORMED'),\n"
                "                ],\n"
                "            ),\n"
                "            pytest.param(\n"
                "                4, marks=pytest.mark.contract(f'EFF-{1}')\n"
                "            ),\n"
                "        ],\n"
                "    )\n"
                "    def test_params(self, value):\n"
                "        pass\n"
            )
        }
    )
    running_ids = {o.id for o in occurrences if o.running and o.id is not None}
    assert running_ids == {"EFF-ISOLATE", "REG-WELLFORMED"}
    skipped_ids = {o.id for o in occurrences if not o.running and o.id is not None}
    assert skipped_ids == {"EFF-REVERT", "LIFE-ORDER"}
    # the f-string marks no ID, and its failure message locates the marked test:
    non_literals = [o for o in occurrences if o.id is None]
    assert len(non_literals) == 1
    assert check_marker_arguments(occurrences)[0].startswith(
        "tests/test_a.py::TestLive::test_params: "
    )


def test_non_string_marker_argument_is_reported() -> None:
    occurrences = scan_markers(
        {
            "tests/test_a.py": (
                "import pytest\n"
                "CASE_ID = 'EFF-REVERT'\n"
                "@pytest.mark.contract(CASE_ID)\n"
                "def test_dynamic():\n"
                "    pass\n"
                "\n"
                "@pytest.mark.contract()\n"
                "def test_no_id():\n"
                "    pass\n"
                "\n"
                "@pytest.mark.contract('EFF-REVERT')\n"
                "@pytest.mark.xfail(reason='negative test of the specification')\n"
                "def test_xfail_reason_is_not_an_id():\n"
                "    pass\n"
            )
        }
    )
    failures = check_marker_arguments(occurrences)
    assert len(failures) == 2
    assert all("needs a string-literal ID argument" in f for f in failures)
    assert any("::test_dynamic: " in f for f in failures)
    assert any("::test_no_id: " in f for f in failures)
    # the xfail reason is not a traceability-marker argument, so the marked test
    # keeps its literal ID and still counts as running:
    assert [
        (o.marker, o.id, o.running)
        for o in occurrences
        if o.location.endswith("test_xfail_reason_is_not_an_id")
    ] == [("contract", "EFF-REVERT", True)]


def test_tables_are_read_from_the_architecture_document() -> None:
    contract_ids = table_section_ids(ARCHITECTURE_MD, CONTRACT_TABLE_HEADER)
    assert contract_ids[:2] == ["EFF-REVERT", "EFF-ISOLATE"]
    assert "LIFE-FAILURE" in contract_ids
    assert table_section_ids(_CONTRACT_TABLE, CONTRACT_TABLE_HEADER) == ["A-1"]
    assert table_section_ids(_DIVERGENCE_TABLE, DIVERGENCE_TABLE_HEADER) == ["DIV-1"]
    assert table_section_ids(
        _CONTRACT_TABLE.replace("| `A-1` |", "| A-1 |"), CONTRACT_TABLE_HEADER
    ) == ["A-1"]
    with pytest.raises(ValueError, match="no table with header"):
        table_section_ids("# no tables here\n", CONTRACT_TABLE_HEADER)


def test_reviewed_entries_pass_every_ledger_check() -> None:
    entries = {
        CASE: {"status": "adapted", "hash": "h1", "divergence": "ADAPT-1"},
        SYMBOL: {
            "status": "ported",
            "hash": "h2",
            "target": "pycordis",
            "mirror": True,
        },
    }
    markers = _upstream_marker()
    assert check_hash_pinning(ITEMS, entries) == []
    assert _status(entries, markers) == []
    assert check_target_resolution(entries) == []
    assert check_upstream_markers(ITEMS, entries, markers) == []


def test_resolves_follows_modules_then_attributes() -> None:
    assert resolves("pycordis")
    assert resolves("tests_missing") is False
    assert resolves("collections.abc.Mapping")
    assert resolves("collections.abc.NoSuchName") is False


def test_readme_revision_is_the_tracked_hash() -> None:
    assert readme_revision(README_MD) == INVENTORY["revision"]
    with pytest.raises(ValueError, match="no tracked upstream revision"):
        readme_revision("# no revision\n")


def test_source_vocabulary_reads_literals_f_strings_and_enum_members() -> None:
    assert {"a", "b {} c"} <= VOCABULARY.strings
    assert "b " not in VOCABULARY.strings  # an f-string fragment is not a literal
    # an Enum base is read as a name or as an attribute; a plain class is not read:
    assert VOCABULARY.enum_members == {"ACTIVE", "ONE"}


def test_vocabulary_accepts_present_names_and_divergent_or_pending_entries() -> None:
    assert _vocabulary("message", "b {} c") == []
    assert _vocabulary("event", "a") == []
    assert _vocabulary("state", "ACTIVE") == []
    assert _vocabulary("message", "gone", divergence="D-1") == []
    assert _vocabulary("message", "gone", status="pending") == []


def test_divergence_rows_with_full_evidence_pass() -> None:
    entries = {"x": {"status": "adapted", "divergence": "D-1"}}
    assert _evidence(_row("adaptation"), entries) == []
    assert _evidence(_row("omission", "", "Not portable."), entries) == []
    assert _evidence(_row("improvement", "EFF-REVERT")) == []
    assert _evidence(_row("correction", "EFF-REVERT", f"See {LINK}")) == []
    pull = LINK.replace("issues", "pull")
    assert _evidence(_row("correction", "EFF-REVERT", f"See {pull}")) == []
    # a marker alone keeps a row in use, with no ledger entry:
    assert _evidence(_row("omission", "", "Not portable.")) == []
