"""SQLite memory (sqlmodel): job history, and per repo what the maintainers told Driftless.

- Jobs: every webhook job, so `GET /runs` survives restarts.
- Ignore rules (`/driftless ignore <file or heading>`): sections and files Driftless leaves alone.
- Style preferences (`/driftless style <note>`): given to the model with every rewrite.
- Feedback: the changes of each docs PR Driftless opened, marked accepted (merged) or rejected
  (closed without merging). Recent rejected ones are shown to the model as what not to do.

Repos are keyed by lower-case `owner/name`; rows are never shared between repos.
"""

import fnmatch
import json
import time
from typing import Any

from pydantic import BaseModel, Field as PydanticField
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, Session, SQLModel, col, create_engine, func, select

from driftless.models import DocSection
from driftless.normalize import normalize_heading

MAX_REJECTED_IN_PROMPT = 3
UNFINISHED = ("queued", "running")


class JobRecord(SQLModel, table=True):
    __tablename__ = "jobs"

    id: str = Field(primary_key=True)
    repo: str = Field(index=True)
    pr: int
    status: str = Field(index=True)
    queued_at: float = Field(index=True)
    data: str  # the whole Job as JSON


class IgnoreRule(SQLModel, table=True):
    __tablename__ = "ignore_rules"

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    pattern: str
    created_by: str
    created_at: float


class StylePreference(SQLModel, table=True):
    __tablename__ = "style_preferences"

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    note: str
    created_by: str
    created_at: float


class DocsPullRecord(SQLModel, table=True):
    """A docs PR Driftless opened, with the changes it proposed (latest version)."""

    __tablename__ = "docs_pulls"

    id: int | None = Field(default=None, primary_key=True)
    repo: str = Field(index=True)
    number: int = Field(index=True)
    source_pr: int
    changes: str  # JSON list of RememberedChange
    outcome: str | None = None  # accepted | rejected
    updated_at: float
    resolved_at: float | None = None


class RememberedChange(BaseModel):
    """One change of a docs PR, as the model will see it when it was rejected."""

    file: str
    section: str
    kind: str  # updated | added | example
    reason: str
    diff: str = ""
    docs_pr: int | None = None


class RepoMemory(BaseModel):
    """What applies to one repo's runs: ignore rules, style notes, recent rejected changes."""

    ignore: list[str] = PydanticField(default_factory=list)
    style: list[str] = PydanticField(default_factory=list)
    rejected: list[RememberedChange] = PydanticField(default_factory=list)

    @property
    def guides_the_model(self) -> bool:
        return bool(self.style or self.rejected)

    def ignoring_rule(self, section: DocSection) -> str | None:
        """The first rule that matches the section's file or one of its headings."""
        headings = {normalize_heading(h) for h in section.heading_path}
        for rule in self.ignore:
            if matches_path(rule, section.path) or normalize_heading(rule) in headings:
                return rule
        return None

    def ignoring_file_rule(self, path: str) -> str | None:
        return next((rule for rule in self.ignore if matches_path(rule, path)), None)


def matches_path(rule: str, path: str) -> bool:
    """`README.md`, a folder (`docs/legacy/`, `docs/legacy`) or a glob (`docs/*.md`)."""
    rule = rule.strip().lstrip("./")
    if not rule:
        return False
    if rule == path or path.startswith(rule.rstrip("/") + "/"):
        return True
    return any(ch in rule for ch in "*?[") and fnmatch.fnmatch(path, rule)


class Memory:
    def __init__(self, url: str = "sqlite:///driftless.db") -> None:
        options: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            options["poolclass"] = StaticPool  # one shared in-memory database across threads
        self._engine = create_engine(url, **options)
        SQLModel.metadata.create_all(self._engine)

    # Jobs ---------------------------------------------------------------------------------

    def save_job(self, job_id: str, repo: str, pr: int, status: str, queued_at: float, data: dict[str, Any]) -> None:
        with Session(self._engine) as session:
            session.merge(JobRecord(id=job_id, repo=repo, pr=pr, status=status, queued_at=queued_at, data=json.dumps(data)))
            session.commit()

    def recent_jobs(self, limit: int = 20, repo: str | None = None) -> list[dict[str, Any]]:
        query = select(JobRecord)
        if repo is not None:
            query = query.where(func.lower(JobRecord.repo) == _key(repo))
        with Session(self._engine) as session:
            rows = session.exec(query.order_by(col(JobRecord.queued_at).desc()).limit(limit)).all()
            return [json.loads(row.data) for row in rows]

    def job(self, job_id: str) -> dict[str, Any] | None:
        with Session(self._engine) as session:
            row = session.get(JobRecord, job_id)
            return json.loads(row.data) if row else None

    def interrupt_unfinished(self) -> int:
        """Jobs left queued or running by a previous process will never finish: mark them failed."""
        with Session(self._engine) as session:
            rows = session.exec(select(JobRecord).where(col(JobRecord.status).in_(UNFINISHED))).all()
            for row in rows:
                data = json.loads(row.data)
                data.update(status="failed", error="interrupted: the server restarted before this job finished")
                row.status, row.data = "failed", json.dumps(data)
                session.add(row)
            session.commit()
            return len(rows)

    # Ignore rules and style preferences ---------------------------------------------------

    def add_ignore(self, repo: str, pattern: str, user: str) -> bool:
        """False if the rule already existed."""
        if pattern in self.ignore_rules(repo):
            return False
        with Session(self._engine) as session:
            session.add(IgnoreRule(repo=_key(repo), pattern=pattern, created_by=user, created_at=time.time()))
            session.commit()
        return True

    def remove_ignore(self, repo: str, pattern: str) -> bool:
        with Session(self._engine) as session:
            rows = session.exec(
                select(IgnoreRule).where(IgnoreRule.repo == _key(repo), IgnoreRule.pattern == pattern)
            ).all()
            for row in rows:
                session.delete(row)
            session.commit()
            return bool(rows)

    def ignore_rules(self, repo: str) -> list[str]:
        with Session(self._engine) as session:
            rows = session.exec(select(IgnoreRule).where(IgnoreRule.repo == _key(repo)).order_by(col(IgnoreRule.id))).all()
            return [row.pattern for row in rows]

    def add_style(self, repo: str, note: str, user: str) -> int:
        """Returns the preference's number, as `/driftless memory` lists it."""
        with Session(self._engine) as session:
            session.add(StylePreference(repo=_key(repo), note=note, created_by=user, created_at=time.time()))
            session.commit()
        return len(self.style_notes(repo))

    def remove_style(self, repo: str, number: int) -> str | None:
        """Remove preference `number` (1-based); its text, or None if there is no such number."""
        with Session(self._engine) as session:
            rows = self._styles(session, repo)
            if not 1 <= number <= len(rows):
                return None
            row = rows[number - 1]
            session.delete(row)
            session.commit()
            return row.note

    def style_notes(self, repo: str) -> list[str]:
        with Session(self._engine) as session:
            return [row.note for row in self._styles(session, repo)]

    # Feedback from docs PRs ---------------------------------------------------------------

    def record_docs_pr(self, repo: str, number: int, source_pr: int, changes: list[RememberedChange]) -> None:
        """Remember what docs PR `number` proposes; a rerun that updates the PR replaces it."""
        payload = json.dumps([c.model_dump() for c in changes])
        with Session(self._engine) as session:
            row = self._docs_pull(session, repo, number) or DocsPullRecord(
                repo=_key(repo), number=number, source_pr=source_pr, changes="[]", updated_at=0
            )
            row.source_pr, row.changes, row.updated_at = source_pr, payload, time.time()
            row.outcome, row.resolved_at = None, None
            session.add(row)
            session.commit()

    def resolve_docs_pr(self, repo: str, number: int, merged: bool) -> list[RememberedChange] | None:
        """Mark a docs PR's changes accepted (merged) or rejected (closed); None if it isn't ours."""
        with Session(self._engine) as session:
            row = self._docs_pull(session, repo, number)
            if row is None:
                return None
            row.outcome, row.resolved_at = ("accepted" if merged else "rejected"), time.time()
            session.add(row)
            session.commit()
            return _changes(row)

    def feedback_counts(self, repo: str) -> dict[str, int]:
        """Docs PRs per outcome: {"accepted": n, "rejected": m}."""
        with Session(self._engine) as session:
            rows = session.exec(select(DocsPullRecord).where(DocsPullRecord.repo == _key(repo))).all()
            return {o: sum(1 for r in rows if r.outcome == o) for o in ("accepted", "rejected")}

    def recent_rejected(self, repo: str, limit: int = MAX_REJECTED_IN_PROMPT) -> list[RememberedChange]:
        with Session(self._engine) as session:
            rows = session.exec(
                select(DocsPullRecord)
                .where(DocsPullRecord.repo == _key(repo), DocsPullRecord.outcome == "rejected")
                .order_by(col(DocsPullRecord.resolved_at).desc())
            ).all()
            changes = [change for row in rows for change in _changes(row)]
            return changes[:limit]

    def repo_memory(self, repo: str) -> RepoMemory:
        return RepoMemory(ignore=self.ignore_rules(repo), style=self.style_notes(repo), rejected=self.recent_rejected(repo))

    @staticmethod
    def _styles(session: Session, repo: str) -> list[StylePreference]:
        query = select(StylePreference).where(StylePreference.repo == _key(repo)).order_by(col(StylePreference.id))
        return list(session.exec(query).all())

    @staticmethod
    def _docs_pull(session: Session, repo: str, number: int) -> DocsPullRecord | None:
        query = select(DocsPullRecord).where(DocsPullRecord.repo == _key(repo), DocsPullRecord.number == number)
        return session.exec(query).first()


def _changes(row: DocsPullRecord) -> list[RememberedChange]:
    return [RememberedChange(**{**c, "docs_pr": row.number}) for c in json.loads(row.changes)]


def _key(repo: str) -> str:
    return repo.lower()
