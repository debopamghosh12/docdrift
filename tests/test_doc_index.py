from driftless.doc_index import PREAMBLE, index_document, is_doc_path
from driftless.models import DocFile

README = """\
Intro text before any heading.

# Project

Overview.

## Setup

```bash
# not a heading
pip install project
```

### Env vars

Set `API_KEY`.

Usage
-----

Run it.
"""


def test_is_doc_path() -> None:
    assert is_doc_path("README.md")
    assert is_doc_path("docs/guide/install.md")
    assert not is_doc_path("src/README.md")
    assert not is_doc_path("docs/image.png")


def test_sections_headings_and_paths() -> None:
    sections = index_document(DocFile(path="README.md", content=README))
    assert [s.heading for s in sections] == [PREAMBLE, "Project", "Setup", "Env vars", "Usage"]
    env = sections[3]
    assert env.heading_path == ["Project", "Setup", "Env vars"]
    assert env.level == 3
    # Setext "Usage" is level 2, so it pops back under "Project".
    assert sections[4].heading_path == ["Project", "Usage"]


def test_fenced_code_is_not_a_heading_and_line_ranges() -> None:
    sections = index_document(DocFile(path="README.md", content=README))
    setup = sections[2]
    assert "# not a heading" in setup.text
    assert setup.start_line == 7
    assert setup.end_line == 13
    lines = README.splitlines()
    assert lines[setup.start_line - 1] == "## Setup"


def test_front_matter_is_skipped() -> None:
    doc = DocFile(path="docs/a.md", content="---\ntitle: A\n---\n# Real\nbody\n")
    sections = index_document(doc)
    assert [s.heading for s in sections] == [PREAMBLE, "Real"]
