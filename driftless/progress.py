"""Stages of a long run, reported as it goes (the web app shows them while a job runs)."""

from collections.abc import Callable
from enum import Enum


class Stage(str, Enum):
    FETCH = "fetch"  # downloading the PR, docs and code
    REVIEW = "review"  # LLM review of candidate sections and missing docs
    START = "start"  # installing the app's dependencies (cached) and starting it
    RUN = "run"  # running the docs' examples against the app (and fixing failures)
    BASELINE = "baseline"  # pre-merge: re-checking drift at the PR's base commit
    PUBLISH = "publish"  # opening a PR, commenting or committing


StageProgress = Callable[[Stage, str], None]


def silent(stage: Stage, detail: str) -> None:
    pass
