"""Tests for the static conformance checks.

Each check must pass on the committed conformance data and must fail with a message
naming the item on a minimal bad input.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from conformance_checks import (
    CONTRACT_TABLE_HEADER,
    DIVERGENCE_TABLE_HEADER,
    MarkerOccurrence,
    check_contract_coverage,
    check_marker_arguments,
    check_marker_ids,
    pending_contract_ids,
    scan_markers,
    table_section_ids,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_MD = (REPO_ROOT / "docs" / "architecture.md").read_text("utf-8")
LEDGER_TOML = (REPO_ROOT / "conformance" / "ledger.toml").read_text("utf-8")


def _suite_sources() -> dict[str, str]:
    """Read every test file, as the static marker scan of the suite does."""
    tests_dir = REPO_ROOT / "tests"
    return {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text("utf-8")
        for path in sorted(tests_dir.rglob("*.py"))
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


def test_checks_pass_on_committed_data() -> None:
    contract_ids = table_section_ids(ARCHITECTURE_MD, CONTRACT_TABLE_HEADER)
    divergence_ids = table_section_ids(ARCHITECTURE_MD, DIVERGENCE_TABLE_HEADER)
    occurrences = _committed_occurrences()
    pending = pending_contract_ids(LEDGER_TOML)
    assert check_marker_arguments(occurrences) == []
    assert check_marker_ids(occurrences, contract_ids, divergence_ids) == []
    assert check_contract_coverage(contract_ids, pending, occurrences) == []


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


def test_divergence_marker_naming_an_unknown_row_fails() -> None:
    occurrences = scan_markers(
        {
            "tests/test_a.py": (
                "import pytest\n"
                "@pytest.mark.divergence('DIV-404')\n"
                "def test_gone():\n"
                "    pass\n"
            )
        }
    )
    assert check_marker_ids(occurrences, [], ["DIV-1"]) == [
        "tests/test_a.py::test_gone: @pytest.mark.divergence names 'DIV-404', "
        "which is not a row of docs/architecture.md"
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
