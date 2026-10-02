"""answer(): retrieve, prompt, cite, refuse, audit. Model and database are faked."""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from sovrag.auth import Clearance, Principal
from sovrag.generate import answer as answer_mod
from sovrag.generate.answer import MAX_K, Answer, Citation, answer, sources_markdown
from sovrag.generate.llm import GenerationError
from sovrag.generate.prompts import REFUSAL_TEXT, SENTINEL
from sovrag.retrieve import Hit, Mode

WHO = Principal(id="analyst_dnd", clearance=Clearance.PROTECTED_A, departments=frozenset({"DND"}))
WHO_FILTER = (
    "classification in (UNCLASSIFIED, PROTECTED_A)"
    " and (classification = UNCLASSIFIED or department in (GC_WIDE, DND))"
)


def _hit(n: int) -> Hit:
    return Hit(
        chunk_id=100 + n,
        doc_id=f"doc-{n}",
        ordinal=n,
        heading_path=f"Part {n}",
        text=f"Body {n}.",
        title=f"Title {n}",
        department="DND",
        classification="UNCLASSIFIED",
        doc_type="DP",
        source_url=f"https://example.test/{n}",
        score=1.0,
    )


class FakeGenerator:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]], *, max_tokens: int | None = None) -> str:
        self.calls.append(list(messages))
        return self.replies.pop(0)


@pytest.fixture
def audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(target))
    return target


class StubEmbedder:
    def embed_one(self, q: str) -> list[float]:
        return [0.0] * 4


def _wire(monkeypatch: pytest.MonkeyPatch, hits: list[Hit], replies: list[str]) -> FakeGenerator:
    gen = FakeGenerator(replies)

    def fake_search(conn: object, q: str, **kw: object) -> list[Hit]:
        assert kw["principal"] is WHO
        if kw["mode"] is not Mode.keyword:
            kw["embed_query"](q)  # type: ignore[operator]
        return list(hits[: kw["k"]])  # type: ignore[misc]

    monkeypatch.setattr(answer_mod.Generator, "from_settings", classmethod(lambda cls, s: gen))
    monkeypatch.setattr(answer_mod, "connect", lambda url: nullcontext(object()))
    monkeypatch.setattr(answer_mod, "search", fake_search)
    monkeypatch.setattr(
        answer_mod.Embedder, "from_settings", classmethod(lambda cls, s: StubEmbedder())
    )
    return gen


def _audit_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_cited_answer_builds_citations_for_cited_hits_only(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    _wire(monkeypatch, [_hit(1), _hit(2), _hit(3)], ["Fact [3]. More [1]."])
    result = answer("q", principal=WHO, mode=Mode.vector, k=3, request_id="r1")
    assert result == Answer(
        question="q",
        principal="analyst_dnd",
        mode="vector",
        request_id="r1",
        text="Fact [3]. More [1].",
        citations=(
            Citation(1, "doc-1", 101, "Title 1", "Part 1", "https://example.test/1"),
            Citation(3, "doc-3", 103, "Title 3", "Part 3", "https://example.test/3"),
        ),
        refused=False,
        model=result.model,
        retrieved_chunk_ids=(101, 102, 103),
        applied_filter=WHO_FILTER,
    )
    (line,) = _audit_lines(audit_path)
    assert line["event"] == "answer"
    assert line["request_id"] == "r1"
    assert line["cited_chunk_ids"] == [101, 103]
    assert line["retrieved_chunk_ids"] == [101, 102, 103]
    assert line["refused"] is False and line["reason"] is None
    assert line["applied_filter"] == WHO_FILTER


def test_sentinel_refuses_with_no_citations(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    _wire(monkeypatch, [_hit(1)], [SENTINEL])
    result = answer("q", principal=WHO)
    assert result.refused and result.text == REFUSAL_TEXT and result.citations == ()
    assert _audit_lines(audit_path)[0]["reason"] == "sentinel"


def test_uncited_reply_is_retried_firmly_then_accepted(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    gen = _wire(monkeypatch, [_hit(1)], ["No markers here.", "Now cited [1]."])
    result = answer("q", principal=WHO)
    assert not result.refused and result.text == "Now cited [1]."
    assert len(gen.calls) == 2
    assert "no citations" in gen.calls[1][0]["content"]
    assert "no citations" not in gen.calls[0][0]["content"]


def test_uncited_twice_refuses_with_reason_uncited(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    _wire(monkeypatch, [_hit(1)], ["Nope.", "Still nope [9]."])
    result = answer("q", principal=WHO)
    assert result.refused and result.citations == ()
    assert _audit_lines(audit_path)[0]["reason"] == "uncited"


def test_no_hits_refuses_without_calling_the_model(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    gen = _wire(monkeypatch, [], ["should not be used"])
    result = answer("q", principal=WHO)
    assert result.refused and result.retrieved_chunk_ids == ()
    assert gen.calls == []
    assert _audit_lines(audit_path)[0]["reason"] == "no_hits"


class BoomGenerator:
    def complete(self, messages: list[dict[str, str]], *, max_tokens: int | None = None) -> str:
        raise GenerationError("boom")


def test_generation_error_is_audited_then_raised(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    monkeypatch.setattr(
        answer_mod.Generator, "from_settings", classmethod(lambda cls, s: BoomGenerator())
    )
    monkeypatch.setattr(answer_mod, "connect", lambda url: nullcontext(object()))
    monkeypatch.setattr(answer_mod, "search", lambda conn, q, **kw: [_hit(1)][: kw["k"]])

    with pytest.raises(GenerationError):
        answer("q", principal=WHO)

    (line,) = _audit_lines(audit_path)
    assert line["reason"] == "error"
    assert line["refused"] is True
    assert line["cited_chunk_ids"] == []
    assert line["retrieved_chunk_ids"] == [101]


def test_k_is_clamped_to_max_k(monkeypatch: pytest.MonkeyPatch, audit_path: Path) -> None:
    seen: list[int] = []
    gen = FakeGenerator(["ok [1]"])
    monkeypatch.setattr(answer_mod.Generator, "from_settings", classmethod(lambda cls, s: gen))
    monkeypatch.setattr(answer_mod, "connect", lambda url: nullcontext(object()))

    def fake_search(conn: object, q: str, **kw: object) -> list[Hit]:
        seen.append(int(kw["k"]))  # type: ignore[call-overload]
        return [_hit(1)]

    monkeypatch.setattr(answer_mod, "search", fake_search)
    answer("q", principal=WHO, k=50)
    assert seen == [MAX_K]
    assert _audit_lines(audit_path)[0]["k"] == MAX_K


def test_keyword_mode_never_builds_the_embedder(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    _wire(monkeypatch, [_hit(1)], ["ok [1]"])

    def boom(cls: type, s: object) -> None:
        raise AssertionError("embedder constructed in keyword mode")

    monkeypatch.setattr(answer_mod.Embedder, "from_settings", classmethod(boom))
    assert not answer("q", principal=WHO, mode=Mode.keyword).refused


def test_vector_mode_builds_the_embedder_once(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    _wire(monkeypatch, [_hit(1)], ["ok [1]"])
    calls: list[object] = []

    class FakeEmbedder:
        def embed_one(self, q: str) -> list[float]:
            return [0.0] * 4

    def from_settings(cls: type, s: object) -> FakeEmbedder:
        calls.append(s)
        return FakeEmbedder()

    monkeypatch.setattr(answer_mod.Embedder, "from_settings", classmethod(from_settings))
    assert not answer("q", principal=WHO, mode=Mode.vector).refused
    assert len(calls) == 1


def test_refusal_still_reports_the_applied_filter(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    _wire(monkeypatch, [], ["unused"])
    result = answer("q", principal=WHO)
    assert result.refused and result.applied_filter == WHO_FILTER
    (line,) = _audit_lines(audit_path)
    assert line["applied_filter"] == WHO_FILTER


def _citation(n: int, title: str, heading_path: str, source_url: str) -> Citation:
    return Citation(
        n=n,
        doc_id="d",
        chunk_id=n,
        title=title,
        heading_path=heading_path,
        source_url=source_url,
    )


def test_sources_markdown_is_a_list_of_links_the_chat_can_render() -> None:
    """One list item per citation: the number, the title as the link text, the URL as the
    target, then the heading path. A chat client renders it as clickable titles."""
    sources = sources_markdown(
        (
            _citation(3, "Results Report", "Report > Details on results", "https://x.test/a"),
            _citation(5, "Audit", "Audit > Findings", "https://x.test/b"),
        )
    )
    assert sources == (
        "- **[3]** [Results Report](https://x.test/a): Report > Details on results\n"
        "- **[5]** [Audit](https://x.test/b): Audit > Findings"
    )


def test_sources_markdown_escapes_brackets_in_titles() -> None:
    sources = sources_markdown((_citation(1, "Plan [draft]", "Plan", "https://x.test/p"),))
    assert sources == "- **[1]** [Plan \\[draft\\]](https://x.test/p): Plan"


def test_sources_markdown_is_empty_without_citations() -> None:
    assert sources_markdown(()) == ""
