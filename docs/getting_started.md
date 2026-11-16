# Getting Started

## Install

pycordis has no runtime dependencies and needs Python 3.12 or later. Install it from PyPI:

```bash
pip install pycordis2
```

With uv:

```bash
uv add pycordis2
```

The PyPI distribution name is `pycordis2`. The import name is `pycordis`.

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
