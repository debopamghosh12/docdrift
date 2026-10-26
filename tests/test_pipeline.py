from io import StringIO

from rich.console import Console

from driftless.models import ChangedFile, DocFile, PullRequestInfo, SourceFile
from driftless.pipeline import analyze_pull_request
from driftless.report import render_analysis


class FakeProvider:
    def fetch_pull_request(self, repo_name: str, number: int, require_merged: bool = True) -> PullRequestInfo:
        return PullRequestInfo(
            repo=repo_name,
            number=number,
            title="Rename connect",
            base_ref="main",
            merge_base_sha="a" * 40,
            head_sha="b" * 40,
            merge_commit_sha="c" * 40,
            files=[
                ChangedFile(
                    path="lib/db.py",
                    before="def connect(url):\n    return url\n",
                    after="def open_connection(url):\n    return url\n",
                )
            ],
        )

    def fetch_docs(self, repo_name: str, ref: str) -> list[DocFile]:
        assert ref == "c" * 40
        return [DocFile(path="README.md", content="# Lib\n\n## Usage\n\nCall `connect(url)`.\n")]

    def fetch_sources(self, repo_name: str, ref: str) -> list[SourceFile]:
        return [SourceFile(path="lib/db.py", content="def open_connection(url):\n    return url\n")]


def test_pipeline_end_to_end() -> None:
    result = analyze_pull_request(FakeProvider(), "me/lib", 7)
    assert [s.name for s in result.changeset.symbols] == ["open_connection"]
    assert "open_connection" in result.code.functions
    assert [c.section.heading for c in result.candidates] == ["Usage"]
    # The old name is documented and that section is a candidate, so it's not "missing".
    assert result.missing == []

    out = StringIO()
    render_analysis(result, Console(file=out, width=160))
    text = out.getvalue()
    assert "connect -> open_connection" in text
    assert "README.md:3-5" in text


def test_pipeline_refuses_driftless_branches() -> None:
    import pytest

    from driftless.pipeline import OwnPullRequestError

    class OwnPR(FakeProvider):
        def fetch_pull_request(self, repo_name: str, number: int, require_merged: bool = True) -> PullRequestInfo:
            return super().fetch_pull_request(repo_name, number).model_copy(update={"head_ref": "driftless/pr-7"})

        def fetch_docs(self, repo_name: str, ref: str) -> list[DocFile]:
            raise AssertionError("must stop before fetching docs")

    with pytest.raises(OwnPullRequestError):
        analyze_pull_request(OwnPR(), "me/lib", 8)
