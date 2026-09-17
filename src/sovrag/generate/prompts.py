"""Prompt construction for grounded answers.

The model sees numbered sources and the question, nothing else. It must cite with [n] and
must reply with the sentinel alone when the sources do not contain the answer; the code
turns the sentinel into a fixed refusal. See ADR 0008.
"""

from __future__ import annotations

from collections.abc import Sequence

from sovrag.retrieve import Hit

SENTINEL = "INSUFFICIENT_CONTEXT"
REFUSAL_TEXT = "The retrieved documents do not contain enough information to answer this question."

SYSTEM = (
    "You answer questions about Government of Canada documents using only the numbered "
    "sources provided. Cite every claim with the source number in square brackets, such as "
    "[1] or [2][3]. Do not use knowledge from outside the sources. Keep the answer short and "
    f"factual. If the sources do not contain the answer, reply with exactly {SENTINEL} and "
    "nothing else."
)
FIRM = (
    "Your previous reply had no citations. Every sentence must end with at least one [n] "
    "citation to a source."
)


def format_sources(hits: Sequence[Hit]) -> str:
    return "\n\n".join(
        f"[{n}] {h.title} > {h.heading_path}\n{h.text}" for n, h in enumerate(hits, start=1)
    )


def build_messages(
    question: str, hits: Sequence[Hit], *, firm: bool = False
) -> list[dict[str, str]]:
    system = f"{SYSTEM} {FIRM}" if firm else SYSTEM
    user = f"Sources:\n\n{format_sources(hits)}\n\nQuestion: {question}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
