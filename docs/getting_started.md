# Getting Started

## Install

pycordis has no runtime dependencies and needs Python 3.12 or later. It is not published to a
package index. Install a tagged release from git.

With uv, pin the tag as a source so that no package index can resolve the name:

```toml
[project]
dependencies = ["pycordis"]

[tool.uv.sources]
pycordis = { git = "https://github.com/uclatommy/pycordis", tag = "vX.Y.Z" }
```

With pip:

```bash
pip install "pycordis @ git+https://github.com/uclatommy/pycordis@vX.Y.Z"
```

!!! warning "Name collisions on PyPI"
    The PyPI projects `cordis` and `python-cordis` are not related to pycordis. Install pycordis
    only from its git repository.

## Use a Local Checkout in Another Project

To test a change to pycordis inside a project that depends on it, install the checkout in
editable mode into that project's environment:

```bash
uv sync
uv pip install -e ../pycordis
```

The editable install lasts until the next `uv sync`. Do not commit a path source for pycordis.

## Set Up a Development Environment

```bash
git clone https://github.com/uclatommy/pycordis
cd pycordis
uv sync
uv run pytest
```

`uv sync` installs the `dev` group (pytest, ruff) and the `docs` group (Zensical,
mkdocstrings). See the [Developer Guide](developer_guide.md) for the workflow.
