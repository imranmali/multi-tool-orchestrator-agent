"""The README is executable documentation: every ``python`` block must run.

Each fenced ``python`` block is executed in a fresh namespace (one test per
block, identified by its line number), every capability section must contain at
least one such block, and every relative link must point at a file or folder
that exists. A link to ``openspec/specs/<capability>/spec.md`` is also accepted
while an active OpenSpec change still provides that capability's delta spec, so
the link is valid both before and after the change is archived. The
requirement counts in the specification index must match the specs, and the
demo's sample output must be an in-order excerpt of the real demo output.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

CAPABILITY_SECTIONS = (
    "Tool registry",
    "Capability routing",
    "Permission scoping",
    "Parallel execution",
    "Conflict resolution",
    "Orchestrator agent",
)

_FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_SPEC_LINK_RE = re.compile(r"^openspec/specs/(?P<capability>[^/]+)/spec\.md$")


@dataclass(frozen=True)
class Block:
    language: str
    line: int  # 1-based line of the opening fence
    code: str
    section: str  # nearest preceding level-2 heading


def _parse(text: str) -> tuple[list[Block], list[str]]:
    """Fenced code blocks, and the README lines outside any fence."""
    blocks: list[Block] = []
    prose: list[str] = []
    section = ""
    fence: str | None = None
    language = ""
    start = 0
    body: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if fence is None:
            match = _FENCE_RE.match(line)
            if match:
                fence = match["fence"]
                language = match["info"].strip().split(" ")[0].lower()
                start = number
                body = []
                continue
            if line.startswith("## "):
                section = line[3:].strip()
            prose.append(line)
        else:
            stripped = line.strip()
            if stripped.startswith(fence[0] * len(fence)) and not stripped.strip(fence[0]):
                blocks.append(Block(language, start, "\n".join(body) + "\n", section))
                fence = None
            else:
                body.append(line)
    assert fence is None, f"README.md: unterminated code fence opened on line {start}"
    return blocks, prose


BLOCKS, PROSE = _parse(README.read_text(encoding="utf-8"))
PYTHON_BLOCKS = [block for block in BLOCKS if block.language == "python"]


def test_readme_has_python_examples() -> None:
    assert len(PYTHON_BLOCKS) >= len(CAPABILITY_SECTIONS)


@pytest.mark.parametrize(
    "block", PYTHON_BLOCKS, ids=[f"README.md:{block.line}" for block in PYTHON_BLOCKS]
)
def test_python_block_runs(block: Block) -> None:
    namespace: dict[str, object] = {"__name__": f"readme_block_{block.line}"}
    # Pad so tracebacks report the real README line numbers.
    source = "\n" * block.line + block.code
    try:
        exec(compile(source, str(README), "exec"), namespace)  # noqa: S102 - the point of the test
    except Exception as exc:  # report which block failed
        raise AssertionError(
            f"README.md python block starting on line {block.line} "
            f"(section {block.section!r}) failed: {type(exc).__name__}: {exc}"
        ) from exc


@pytest.mark.parametrize("section", CAPABILITY_SECTIONS)
def test_every_capability_section_has_an_example(section: str) -> None:
    headings = {line[3:].strip() for line in PROSE if line.startswith("## ")}
    assert section in headings, f"README.md has no '## {section}' section"
    assert any(block.section == section for block in PYTHON_BLOCKS), (
        f"README.md section '## {section}' has no runnable python block"
    )


def _relative_links() -> list[str]:
    links: list[str] = []
    for line in PROSE:
        for match in _LINK_RE.finditer(line):
            target = match["target"]
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue  # absolute URL or in-page anchor
            links.append(target.split("#", 1)[0])
    return links


def _link_exists(target: str) -> bool:
    if (ROOT / target).exists():
        return True
    match = _SPEC_LINK_RE.match(target)
    if match is None:
        return False
    # Active changes live directly under openspec/changes/ (archived ones one level deeper).
    pattern = f"*/specs/{match['capability']}/spec.md"
    return any((ROOT / "openspec" / "changes").glob(pattern))


def test_relative_links_exist() -> None:
    links = _relative_links()
    assert links, "expected the README to link to its specs"
    missing = [target for target in links if not _link_exists(target)]
    assert not missing, f"README.md links to missing paths: {missing}"


def _slug(heading: str) -> str:
    """GitHub-style anchor for a heading."""
    text = re.sub(r"`", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def test_table_of_contents_anchors_exist() -> None:
    anchors = {_slug(line.lstrip("#")) for line in PROSE if re.match(r"^#{1,6} ", line)}
    targets = [
        match["target"][1:]
        for line in PROSE
        for match in _LINK_RE.finditer(line)
        if match["target"].startswith("#")
    ]
    assert targets, "expected a table of contents"
    missing = [target for target in targets if target not in anchors]
    assert not missing, f"README.md links to missing anchors: {missing}"


def test_spec_index_lists_every_spec() -> None:
    linked = {
        match["capability"]
        for target in _relative_links()
        if (match := _SPEC_LINK_RE.match(target)) is not None
    }
    on_disk = {path.parent.name for path in (ROOT / "openspec" / "specs").glob("*/spec.md")}
    assert on_disk <= linked, f"specs missing from the README index: {sorted(on_disk - linked)}"


def _spec_file(capability: str) -> Path | None:
    """The capability's main spec, or the delta spec of the active change providing it."""
    main = ROOT / "openspec" / "specs" / capability / "spec.md"
    if main.exists():
        return main
    return next(iter(sorted((ROOT / "openspec" / "changes").glob(f"*/specs/{capability}/spec.md"))), None)


def test_spec_index_requirement_counts() -> None:
    row_re = re.compile(r"^\| [^|]+ \| (?P<count>\d+) \| \[(?P<target>openspec/specs/[^\]]+)\]")
    rows = [match for line in PROSE if (match := row_re.match(line)) is not None]
    assert rows, "expected a specification index table"
    wrong = []
    for row in rows:
        capability = _SPEC_LINK_RE.match(row["target"])["capability"]
        spec = _spec_file(capability)
        assert spec is not None, f"no spec found for {capability}"
        actual = sum(1 for line in spec.read_text(encoding="utf-8").splitlines()
                     if line.startswith("### Requirement:"))
        if int(row["count"]) != actual:
            wrong.append(f"{capability}: README says {row['count']}, spec has {actual}")
    assert not wrong, "; ".join(wrong)


def test_demo_sample_output_matches_real_output() -> None:
    from orchestrator.examples import run_demo

    samples = [b for b in BLOCKS if b.section == "Running the demo" and b.language == "text"]
    assert len(samples) == 1, "expected one text block of sample demo output"
    expected = [line.rstrip() for line in samples[0].code.splitlines() if line.strip() != "..."]
    actual = [line.rstrip() for line in asyncio.run(run_demo()).splitlines()]
    position = 0
    for line in expected:  # every sample line appears in the real output, in order
        try:
            position = actual.index(line, position) + 1
        except ValueError:
            raise AssertionError(f"sample demo line not found (in order) in real output: {line!r}") from None
