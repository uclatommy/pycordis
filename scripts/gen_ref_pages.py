"""Generate the API reference pages and the generated navigation in zensical.toml.

Run this script before each documentation build:

    uv run python scripts/gen_ref_pages.py
    uv run zensical build

The script does two tasks:

1. It writes one `docs/api/<module>.md` page for each public module in `src/pycordis/`.
   A module is public when no part of its path starts with `_`. The pages are generated
   output: edit the source docstrings, not the pages.
2. It rewrites the two generated sections of the `nav` list in `zensical.toml`: the API
   reference (between the `API_NAV` markers) and the build specs (between the
   `BUILD_SPECS_NAV` markers). Do not edit these sections by hand.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pycordis"
DOCS = ROOT / "docs"
API = DOCS / "api"
BUILD_SPECS = DOCS / "build_specs"
ZENSICAL = ROOT / "zensical.toml"

_HAND_WRITTEN = {"index.md"}


def _title(path: Path) -> str:
    """Return a readable nav title for a path without its suffix."""
    return " / ".join(
        part.replace("_", " ").replace("-", " ").title()
        for part in path.with_suffix("").parts
    )


def _entry(indent: str, title: str, page: Path) -> str:
    """Return one TOML inline-table nav entry for a page under `docs/`."""
    return f'{indent}{{ "{title}" = "{page.relative_to(DOCS).as_posix()}" }},\n'


def write_api_pages() -> list[Path]:
    """Write one page per public module and remove pages of deleted modules.

    Returns:
        The written pages, in source order.
    """
    for page in API.rglob("*.md"):
        if page.relative_to(API).as_posix() not in _HAND_WRITTEN:
            page.unlink()
    for directory in sorted(API.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()

    pages: list[Path] = []
    for source in sorted(SRC.rglob("*.py")):
        rel = source.relative_to(SRC)
        if any(part.startswith("_") for part in rel.parts):
            continue
        module = ".".join(("pycordis", *rel.with_suffix("").parts))
        page = API / rel.with_suffix(".md")
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"# {rel.stem}\n\n::: {module}\n", encoding="utf-8")
        pages.append(page)
    return pages


def api_nav(pages: list[Path]) -> list[str]:
    """Return the API reference nav section for the generated pages."""
    lines = ['  { "API Reference" = [\n']
    lines.append(_entry("    ", "Overview", API / "index.md"))
    lines += [_entry("    ", _title(page.relative_to(API)), page) for page in pages]
    lines.append("  ]},\n")
    return lines


def build_specs_nav() -> list[str]:
    """Return the build specs nav section: open specs, then one group per folder."""
    lines = ['  { "Build Specs" = [\n']
    lines.append(_entry("    ", "Overview", BUILD_SPECS / "index.md"))
    for spec in sorted(BUILD_SPECS.glob("*.md")):
        if spec.name not in _HAND_WRITTEN:
            lines.append(_entry("    ", _title(Path(spec.name)), spec))
    for folder in sorted(p for p in BUILD_SPECS.iterdir() if p.is_dir()):
        specs = sorted(folder.glob("*.md"))
        if not specs:
            continue
        lines.append(f'    {{ "{_title(Path(folder.name))}" = [\n')
        lines += [_entry("      ", _title(Path(spec.name)), spec) for spec in specs]
        lines.append("    ]},\n")
    lines.append("  ]},\n")
    return lines


def replace_section(text: str, marker: str, body: list[str]) -> str:
    """Replace the lines between the `<marker>_START` and `<marker>_END` comments.

    Raises:
        ValueError: If a marker is missing or the end marker comes first.
    """
    start = text.find(f"  # {marker}_START")
    end = text.find(f"  # {marker}_END")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"{marker} markers not found in {ZENSICAL.name}")
    after_start = text.index("\n", start) + 1
    return text[:after_start] + "".join(body) + text[end:]


def main() -> None:
    """Regenerate the API pages and both generated nav sections."""
    pages = write_api_pages()
    text = ZENSICAL.read_text(encoding="utf-8")
    text = replace_section(text, "API_NAV", api_nav(pages))
    text = replace_section(text, "BUILD_SPECS_NAV", build_specs_nav())
    ZENSICAL.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
