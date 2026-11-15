# pycordis Documentation

pycordis is a Python port of the core package of [Cordis](https://github.com/cordiverse/cordis).
It composes plugins and services from revertible effects and declared dependencies.

## Quick Links

- **[Getting Started](getting_started.md)**: install pycordis and set up a development environment
- **[Developer Guide](developer_guide.md)**: workflow, tests, upstream porting, releases
- **[Contributing](contributing.md)**: rules a change must meet, with the pull request checklist
- **[Architecture](architecture.md)**: design principles, background, divergences from upstream
- **[API Reference](api/index.md)**: generated from the source docstrings
- **[Build Specs](build_specs/index.md)**: design documents for work in progress

## Documentation Structure

```
docs/
├── README.md            # Home page and documentation guide (this file)
├── getting_started.md   # Install and development setup
├── developer_guide.md   # Workflow, tests, porting, releases
├── contributing.md      # Standards for a change, pull request checklist
├── architecture.md      # Principles, background, divergences from upstream
├── api/
│   ├── index.md         # Hand-written API overview
│   └── <module>.md      # Generated, one page per public module (not committed)
└── build_specs/         # Build specs; closed specs move to completed/
zensical.toml            # Site configuration (repository root)
scripts/gen_ref_pages.py # Generates API pages and nav sections
```

## Build the Documentation

```bash
uv run python scripts/gen_ref_pages.py
uv run zensical build
```

Open `site/index.html`, or run `uv run zensical serve` to rebuild on each edit.

## Maintenance Philosophy

**We maintain:**

- Guides that explain concepts, intent, and design decisions. These change slowly.
- The divergence table in [Architecture](architecture.md#divergences-from-upstream). Every
  difference from upstream behavior has a row.

**We do not maintain:**

- API details in the guides. Docstrings hold them, and the API reference is generated from the
  docstrings.
- Implementation details that change often.
- Step-by-step tool instructions that examples or docstrings already give.

## Documentation Principles

1. **Single source**: each fact has one home. A guide links to the API reference; it does not
   copy a signature.
2. **Generated where possible**: API pages and the API and build-spec navigation are generated.
3. **Progressive disclosure**: start with the concept, then add detail.
4. **Present state**: a page describes what the code does now, not its history.

## Contributing to Documentation

| To document | Put it in |
|---|---|
| A public function, class, or module | Its docstring |
| A concept or usage pattern | [Getting Started](getting_started.md) or a guide page |
| A design decision or invariant | [Architecture](architecture.md) |
| A difference from upstream | The divergence table in [Architecture](architecture.md#divergences-from-upstream) |
| A development procedure | [Developer Guide](developer_guide.md) |

A new guide page needs an entry in the hand-written part of the `nav` list in `zensical.toml`.
New modules and build specs need no nav edit: `scripts/gen_ref_pages.py` adds them.
