# pycordis Documentation

pycordis is a Python implementation of the Cordis programming model, with the core package of
[Cordis](https://github.com/cordiverse/cordis) as its reference implementation. It composes
plugins and services from revertible effects and declared dependencies.

## Quick Links

- **[Getting Started](getting_started.md)**: install pycordis and set up a development environment
- **[User Guide](user_guide.md)**: concepts, schedulers, the host contract, and the service
  binding rule
- **[Developer Guide](developer_guide.md)**: workflow, tests, upstream porting, releases
- **[Contributing](contributing.md)**: rules a change must meet, with the pull request checklist
- **[Architecture](architecture.md)**: design principles, the semantic contract, conformance,
  divergences from upstream
- **[API Reference](api/index.md)**: generated from the source docstrings
- **[Build Specs](build_specs/index.md)**: design documents for work in progress

## Documentation Structure

```
docs/
├── README.md            # Home page and documentation guide (this file)
├── getting_started.md   # Install and development setup
├── user_guide.md        # Concepts, schedulers, host contract, binding rule
├── developer_guide.md   # Workflow, tests, porting, releases
├── contributing.md      # Standards for a change, pull request checklist
├── architecture.md      # Principles, semantic contract, conformance, divergences
├── api/
│   ├── index.md         # Hand-written API overview: paper-to-code map
│   └── <module>.md      # Generated, one page per public module (not committed)
├── javascripts/
│   ├── mathjax.js       # MathJax configuration for LaTeX math
│   └── vendor/mathjax/  # MathJax tex-svg.js and its license (third-party, Apache-2.0)
└── build_specs/         # Build specs; closed specs move to completed/
zensical.toml            # Site configuration (repository root)
scripts/gen_ref_pages.py # Generates API pages and nav sections
```

## Build the Documentation

The published site is at <https://uclatommy.github.io/pycordis/>. To build it locally:

```bash
uv run python scripts/gen_ref_pages.py
uv run zensical build --strict
```

`--strict` fails the build on any warning, so a local build applies the same gate as CI.
Open `site/index.html`, or run `uv run zensical serve` to rebuild on each edit.

## Math Rendering

Pages write LaTeX math between `$` signs (inline) or `$$` lines (display), and
`pymdownx.arithmatex` marks it for MathJax. The site serves its own copy of MathJax, so a page
needs no outside host to show a formula. The copy is `docs/javascripts/vendor/mathjax/tex-svg.js`
(MathJax 3.2.2), which draws formulas as SVG and needs no font files.

To update MathJax:

1. Download `es5/tex-svg.js` and `LICENSE` from the `mathjax` npm package at an exact 3.x
   version. MathJax 4 is not a direct replacement: its `tex-svg.js` loads font data and
   speech rules from a CDN at run time, so an offline copy also needs about 10 MB of font
   files and a change to the configuration.
2. Replace the two files in `docs/javascripts/vendor/mathjax/`, and change the version above.
3. Run `uv run zensical build --strict`, and check the formulas on the
   [API Reference](api/index.md) overview page in a browser.

## Maintenance Philosophy

**We maintain:**

- Guides that explain concepts, intent, and design decisions. These change slowly.
- The [semantic contract](architecture.md#semantic-contract) and the divergence table in
  [Architecture](architecture.md#divergences-from-upstream). Every difference from upstream
  behavior has a row.
- The conformance ledger in `conformance/`. Every upstream test case and symbol has an entry.

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
| A guarantee that pycordis keeps | The [semantic contract](architecture.md#semantic-contract) |
| A difference from upstream | The divergence table in [Architecture](architecture.md#divergences-from-upstream) |
| How an upstream item maps to pycordis | `conformance/ledger.toml` (never a docstring) |
| A development procedure | [Developer Guide](developer_guide.md) |

A new guide page needs an entry in the hand-written part of the `nav` list in `zensical.toml`.
New modules and build specs need no nav edit: `scripts/gen_ref_pages.py` adds them.
