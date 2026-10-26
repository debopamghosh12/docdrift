"""JSON-mode chat client for an OpenAI-compatible endpoint, with pydantic validation."""

from collections.abc import Callable
from typing import Any, TypeVar

from openai import APIError, BadRequestError, OpenAI
from pydantic import BaseModel

from docdrift.config import Settings
from docdrift.rate_limit import TokenRateLimiter

M = TypeVar("M", bound=BaseModel)
Check = Callable[[M], None]

CHARS_PER_TOKEN = 3.5
OUTPUT_TOKEN_ALLOWANCE = 1500  # reasoning + answer; settled with real usage afterwards


class LLMError(RuntimeError):
    pass


class JsonLLM:
    """Every call requests `response_format={"type": "json_object"}` and validates the reply.

    On invalid output the call is retried once with the validation error appended, so the
    model can correct itself. `check` adds semantic validation beyond the schema.
    """

    def __init__(
        self,
        client: OpenAI,
        model: str,
        retries: int = 1,
        limiter: TokenRateLimiter | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._retries = retries
        self._limiter = limiter
        self._reasoning_effort = reasoning_effort

    @classmethod
    def from_settings(cls, settings: Settings) -> "JsonLLM":
        if not settings.llm_api_key:
            raise LLMError("LLM_API_KEY is not set")
        client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, max_retries=4)
        limiter = TokenRateLimiter(settings.llm_tpm_limit) if settings.llm_tpm_limit else None
        return cls(
            client,
            settings.llm_model,
            limiter=limiter,
            reasoning_effort=settings.llm_reasoning_effort or None,
        )

    def complete(
        self,
        system: str,
        user: str,
        schema: type[M],
        check: Check[M] | None = None,
        output_tokens: int = OUTPUT_TOKEN_ALLOWANCE,
    ) -> M:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        error = ""
        for _ in range(self._retries + 1):
            raw, error = self._call(messages, output_tokens)
            if raw is not None:
                try:
                    result = schema.model_validate_json(raw)
                    if check:
                        check(result)
                    return result
                except ValueError as exc:  # pydantic.ValidationError is a ValueError
                    error = str(exc)
                messages = messages + [{"role": "assistant", "content": raw}]
            messages = messages + [
                {
                    "role": "user",
                    "content": f"Your previous reply was invalid:\n{error}\n\n"
                    "Reply again with only a corrected JSON object of the required shape.",
                }
            ]
        raise LLMError(f"invalid LLM output after {self._retries + 1} attempts: {error}")

    def _call(self, messages: list[dict[str, str]], output_tokens: int) -> tuple[str | None, str]:
        prompt_chars = sum(len(m["content"]) for m in messages)
        ticket = None
        if self._limiter:
            ticket = self._limiter.acquire(int(prompt_chars / CHARS_PER_TOKEN) + output_tokens)
        extra: dict[str, Any] = {}
        if self._reasoning_effort:
            extra["reasoning_effort"] = self._reasoning_effort
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,  # type: ignore[arg-type]
                response_format={"type": "json_object"},
                **extra,
            )
        except BadRequestError as exc:
            # Groq rejects replies that aren't valid JSON with a 400 (json_validate_failed).
            return None, f"reply was not valid JSON ({exc.message})"
        except APIError as exc:
            raise LLMError(f"LLM API error: {exc}") from exc
        if self._limiter and ticket and response.usage:
            self._limiter.settle(ticket, response.usage.total_tokens)
        return response.choices[0].message.content or "", ""
