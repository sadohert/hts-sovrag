"""Embeddings through the OpenAI-compatible /embeddings endpoint.

Docker Model Runner serves BGE-M3 by default (ADR 0005); any OpenAI-compatible server is a
base-URL swap. Every vector is checked against the configured dimension so a wrong model
fails loudly here rather than at insert time.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx2
from openai import OpenAI

from sovrag.config import Settings


class Embedder:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dim: int,
        api_key: str = "not-needed",
        batch_size: int = 32,
        http_client: httpx2.Client | None = None,
    ) -> None:
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self._client = OpenAI(base_url=base_url, api_key=api_key, http_client=http_client)

    @classmethod
    def from_settings(cls, settings: Settings) -> Embedder:
        return cls(
            base_url=settings.embed_base_url,
            model=settings.embed_model,
            dim=settings.embed_dim,
            api_key=settings.embed_api_key,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            response = self._client.embeddings.create(model=self.model, input=batch)
            items = sorted(response.data, key=lambda d: d.index)
            for item in items:
                if len(item.embedding) != self.dim:
                    raise ValueError(
                        f"embedding dimension {len(item.embedding)} != configured {self.dim}"
                    )
                out.append(list(item.embedding))
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
