"""The Python examples in the README and the user guide run as written."""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FENCE = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)


@pytest.mark.parametrize("page", ["README.md", "docs/user_guide.md"])
def test_the_python_examples_of_a_page_run_in_order(
    page: str, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (REPO_ROOT / page).read_text("utf-8")
    blocks = FENCE.findall(text)
    assert blocks
    namespace: dict[str, object] = {"__name__": "__doc_example__"}

    for block in blocks:
        exec(compile(block, page, "exec"), namespace)  # noqa: S102

    assert capsys.readouterr().out
