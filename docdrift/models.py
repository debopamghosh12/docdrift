"""Shared pydantic models passed between pipeline stages."""

from enum import Enum

from pydantic import BaseModel, Field


class FileStatus(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    RENAMED = "renamed"


class ChangedFile(BaseModel):
    """One file touched by a PR. `before`/`after` hold full content when it was fetched."""

    path: str
    status: FileStatus = FileStatus.MODIFIED
    previous_path: str | None = None
    patch: str = ""
    before: str | None = None
    after: str | None = None


class PullRequestInfo(BaseModel):
    repo: str
    number: int
    title: str
    body: str = ""
    author: str = ""
    url: str = ""
    base_ref: str
    merge_base_sha: str
    head_sha: str
    merge_commit_sha: str
    files: list[ChangedFile] = Field(default_factory=list)


class DocFile(BaseModel):
    path: str
    content: str


class SourceFile(BaseModel):
    path: str
    content: str


class ChangeKind(str, Enum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    SIGNATURE_CHANGED = "signature_changed"
    RENAMED = "renamed"


class SymbolKind(str, Enum):
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"


class SymbolChange(BaseModel):
    name: str
    kind: SymbolKind
    change: ChangeKind
    file: str
    old_name: str | None = None
    old_signature: str | None = None
    new_signature: str | None = None
    public: bool = True
    route: str | None = None  # "METHOD /path" when this function is an HTTP route handler


class EnvVarChange(BaseModel):
    name: str
    change: ChangeKind
    file: str


class RouteChange(BaseModel):
    method: str
    path: str
    change: ChangeKind
    file: str
    handler: str | None = None
    old_method: str | None = None
    old_path: str | None = None

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def old_label(self) -> str | None:
        return f"{self.old_method} {self.old_path}" if self.old_path else None


class CliFlagChange(BaseModel):
    flag: str
    change: ChangeKind
    file: str


class ChangeSet(BaseModel):
    """Doc-relevant changes extracted from a PR's diff."""

    files: list[str] = Field(default_factory=list)
    symbols: list[SymbolChange] = Field(default_factory=list)
    env_vars: list[EnvVarChange] = Field(default_factory=list)
    routes: list[RouteChange] = Field(default_factory=list)
    cli_flags: list[CliFlagChange] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.symbols or self.env_vars or self.routes or self.cli_flags)


class DocSection(BaseModel):
    """A heading-delimited chunk of a markdown file. Line numbers are 1-based, inclusive."""

    path: str
    heading: str
    heading_path: list[str]
    level: int
    start_line: int
    end_line: int
    text: str

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


class Candidate(BaseModel):
    section: DocSection
    score: float
    matched_terms: list[str]


class MissingDoc(BaseModel):
    """A new public interface that no doc section mentions."""

    kind: str
    name: str
    file: str
