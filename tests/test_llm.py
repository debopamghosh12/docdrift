from types import SimpleNamespace

import pytest

from docdrift.analyzer import SectionVerdict
from docdrift.llm import JsonLLM, LLMError
from docdrift.prompts import render_prompt
from docdrift.rate_limit import TokenRateLimiter


class FakeCompletions:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.replies.pop(0)
        usage = SimpleNamespace(total_tokens=100)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


def fake_llm(replies: list[str], **kwargs) -> tuple[JsonLLM, FakeCompletions]:
    completions = FakeCompletions(replies)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return JsonLLM(client, "test-model", **kwargs), completions  # type: ignore[arg-type]


VALID = '{"stale": false, "confidence": 0.9, "reason": "fine", "updated_section": null}'


def test_requests_json_mode_and_validates() -> None:
    llm, completions = fake_llm([VALID], reasoning_effort="low")
    verdict = llm.complete("sys", "user", SectionVerdict)
    assert verdict.stale is False
    call = completions.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["reasoning_effort"] == "low"


def test_retries_once_with_validation_error() -> None:
    invalid = '{"stale": true, "confidence": 0.9, "reason": "x", "updated_section": null}'
    llm, completions = fake_llm([invalid, VALID])
    assert llm.complete("sys", "user", SectionVerdict).stale is False
    retry_messages = completions.calls[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": invalid}
    assert "updated_section must be a non-empty string" in retry_messages[-1]["content"]


def test_gives_up_after_one_retry() -> None:
    llm, completions = fake_llm(["not json", '{"stale": "maybe"}'])
    with pytest.raises(LLMError):
        llm.complete("sys", "user", SectionVerdict)
    assert len(completions.calls) == 2


def test_check_hook_failure_triggers_retry() -> None:
    llm, completions = fake_llm([VALID, VALID])
    seen: list[int] = []

    def check(_: SectionVerdict) -> None:
        seen.append(1)
        if len(seen) == 1:
            raise ValueError("heading does not exist")

    llm.complete("sys", "user", SectionVerdict, check)
    assert "heading does not exist" in completions.calls[1]["messages"][-1]["content"]


def test_rate_limiter_waits_for_window() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = TokenRateLimiter(1000, clock=lambda: now[0], sleep=sleep)
    first = limiter.acquire(800)
    limiter.settle(first, 900)
    limiter.acquire(500)  # 900 + 500 > 1000: must wait until the first call leaves the window
    assert len(sleeps) == 1 and now[0] >= 60.0


def test_prompts_render_with_name_placeholder() -> None:
    text = render_prompt(
        "missing_doc", kind="route", name="PATCH /x", file="a.py", pr_title="t", changes="c",
        hunks="h", outline="o", example_location="l", example="e",
    )
    assert "`PATCH /x`" in text and "$" not in text.split("## Pull request")[0]
