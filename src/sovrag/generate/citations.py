"""Citation markers in generated text.

The model cites sources as [n]. Only markers that point at a provided source are kept; the
rest are removed so the returned text never carries a citation without a Citation object.
"""

from __future__ import annotations

import re

from sovrag.generate.prompts import SENTINEL

_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([.,;:!?])")
_DOUBLE_SPACE = re.compile(r"[ \t]{2,}")


def is_refusal(text: str) -> bool:
    return SENTINEL in text


def parse(text: str, k: int) -> tuple[str, tuple[int, ...]]:
    cited: set[int] = set()

    def replace(match: re.Match[str]) -> str:
        keep = [n for n in (int(x) for x in match.group(1).split(",")) if 1 <= n <= k]
        cited.update(keep)
        return "".join(f"[{n}]" for n in keep)

    cleaned = _MARKER.sub(replace, text)
    cleaned = _SPACE_BEFORE_PUNCT.sub(r"\1", cleaned)
    cleaned = _DOUBLE_SPACE.sub(" ", cleaned).strip()
    return cleaned, tuple(sorted(cited))
