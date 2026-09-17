"""Chat completions through the OpenAI-compatible client.

Docker Model Runner serves Qwen 3.5 by default (ADR 0006); vLLM or any other OpenAI-compatible
server is a base-URL swap. Qwen 3.x thinks before answering unless told not to, and with a
small token budget the thinking consumes the whole reply, so the thinking flag is always sent.
Temperature 0 keeps answers reproducible for the eval.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import httpx2
from openai import OpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam

from sovrag.config import Settings

THINKING_OFF = {"chat_template_kwargs": {"enable_thinking": False}}


class GenerationError(RuntimeError):
    """The generator could not produce a reply. The message says why."""


class Generator:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "not-needed",
        max_tokens: int = 600,
        max_retries: int = 2,
        timeout_s: float = 180.0,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
            max_retries=max_retries,
            timeout=httpx2.Timeout(timeout_s, connect=5.0),
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Generator:
        return cls(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            max_retries=settings.llm_max_retries,
            timeout_s=settings.llm_timeout_s,
        )

    def complete(self, messages: Sequence[dict[str, str]], *, max_tokens: int | None = None) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=cast("list[ChatCompletionMessageParam]", list(messages)),
                temperature=0,
                max_tokens=max_tokens or self.max_tokens,
                extra_body=THINKING_OFF,
            )
        except OpenAIError as exc:
            raise GenerationError(f"generation failed: {exc}") from exc
        content = response.choices[0].message.content if response.choices else None
        if content is None or not content.strip():
            raise GenerationError("empty reply from the generator")
        return content.strip()
