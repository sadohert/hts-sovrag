"""Grounded answers: retrieve, prompt, cite, refuse, audit.

This is the one entry point for answering. The CLI, the HTTP API and the MCP server all call
it, so the citation rule, the refusal rule and the audit line are enforced in one place.
The principal is passed in by the caller, which is responsible for binding it (ADR on
principal binding). Entitlement filtering will happen inside the search functions.
"""

from __future__ import annotations

from dataclasses import dataclass

from sovrag.audit import write_event
from sovrag.auth import Principal
from sovrag.config import get_settings
from sovrag.generate.citations import is_refusal, parse
from sovrag.generate.llm import GenerationError, Generator
from sovrag.generate.prompts import REFUSAL_TEXT, build_messages
from sovrag.index.embed import Embedder
from sovrag.index.store import connect
from sovrag.retrieve import Hit, Mode, search

MAX_K = 10


@dataclass(frozen=True)
class Citation:
    n: int
    doc_id: str
    chunk_id: int
    title: str
    heading_path: str
    source_url: str


@dataclass(frozen=True)
class Answer:
    question: str
    principal: str
    mode: str
    request_id: str | None
    text: str
    citations: tuple[Citation, ...]
    refused: bool
    model: str
    retrieved_chunk_ids: tuple[int, ...]
    applied_filter: str | None


def answer(
    question: str,
    *,
    principal: Principal,
    mode: Mode = Mode.hybrid,
    k: int = 5,
    request_id: str | None = None,
) -> Answer:
    settings = get_settings()
    k = max(1, min(k, MAX_K))
    with connect(settings.database_url) as conn:
        hits: list[Hit] = search(
            conn,
            question,
            mode=mode,
            k=k,
            # entitlement: pass principal here once the filter lands
            embed_query=lambda q: Embedder.from_settings(settings).embed_one(q),
        )

    def audit(*, cited_chunk_ids: list[int], refused: bool, reason: str | None) -> None:
        write_event(
            "answer",
            request_id=request_id,
            principal=principal.id,
            mode=mode.value,
            k=k,
            model=settings.llm_model,
            retrieved_chunk_ids=[h.chunk_id for h in hits],
            cited_chunk_ids=cited_chunk_ids,
            refused=refused,
            reason=reason,
            applied_filter=None,
        )

    def finish(
        text: str, citations: tuple[Citation, ...], *, refused: bool, reason: str | None
    ) -> Answer:
        result = Answer(
            question=question,
            principal=principal.id,
            mode=mode.value,
            request_id=request_id,
            text=text,
            citations=citations,
            refused=refused,
            model=settings.llm_model,
            retrieved_chunk_ids=tuple(h.chunk_id for h in hits),
            applied_filter=None,
        )
        audit(cited_chunk_ids=[c.chunk_id for c in citations], refused=refused, reason=reason)
        return result

    def refuse(reason: str) -> Answer:
        return finish(REFUSAL_TEXT, (), refused=True, reason=reason)

    if not hits:
        return refuse("no_hits")

    try:
        generator = Generator.from_settings(settings)
        text = generator.complete(build_messages(question, hits))
        if is_refusal(text):
            return refuse("sentinel")
        cleaned, cited = parse(text, len(hits))
        if not cited:
            text = generator.complete(build_messages(question, hits, firm=True))
            if is_refusal(text):
                return refuse("sentinel")
            cleaned, cited = parse(text, len(hits))
            if not cited:
                return refuse("uncited")
    except GenerationError:
        audit(cited_chunk_ids=[], refused=True, reason="error")
        raise

    citations = tuple(
        Citation(
            n=n,
            doc_id=hits[n - 1].doc_id,
            chunk_id=hits[n - 1].chunk_id,
            title=hits[n - 1].title,
            heading_path=hits[n - 1].heading_path,
            source_url=hits[n - 1].source_url,
        )
        for n in cited
    )
    return finish(cleaned, citations, refused=False, reason=None)
