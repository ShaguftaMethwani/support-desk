import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.corpus.retrieve import RetrievedChunk, SearchTrace
from app.llm import LLMResult

DATA = Path(__file__).parent / "data"
FIXTURE_CSV = DATA / "reviews_fixture.csv"
NO_FEES_CSV = DATA / "reviews_no_fees.csv"

Handler = Callable[[Sequence[Mapping[str, str]]], str | Exception]


class ScriptedLLM:
    """Answers each task with a handler: a function of the messages, or a list used in turn.
    Output is validated against the schema like the real client (without the repair retry)."""

    def __init__(self, handlers: Mapping[str, Handler | list[str | Exception]]) -> None:
        self.handlers = dict(handlers)
        self.calls: list[tuple[str, Sequence[Mapping[str, str]]]] = []

    async def complete(
        self,
        task: str,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: type[BaseModel] | None = None,
        json_mode: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> LLMResult:
        self.calls.append((task, messages))
        handler = self.handlers[task]
        out = handler.pop(0) if isinstance(handler, list) else handler(messages)
        if isinstance(out, BaseException):
            raise out
        parsed = schema.model_validate(json.loads(out)) if schema is not None else None
        return LLMResult(out, task, "fake", 0, 0, False, 0, parsed)

    def count(self, task: str) -> int:
        return sum(1 for t, _ in self.calls if t == task)


def theme_label_handler(messages: Sequence[Mapping[str, str]]) -> str:
    text = messages[-1]["content"].lower()
    if "upi" in text:
        label = "Payment and UPI failures"
    elif "support" in text or "ticket" in text:
        label = "Unresponsive customer support"
    elif "exit load" in text:
        label = "Exit load confusion"
    else:
        label = "Slow and crashing app"
    return json.dumps({"label": label, "description": f"Users report {label.lower()}."})


def first_quotes_handler(messages: Sequence[Mapping[str, str]]) -> str:
    """Picks the first candidate listed under each theme."""
    ids = []
    for block in messages[-1]["content"].split("\n\n"):
        for line in block.splitlines():
            if line.strip().startswith("Q"):
                ids.append(line.strip().split(":")[0])
                break
    return json.dumps({"picks": ids[:3]})


def pulse_handler(messages: Sequence[Mapping[str, str]]) -> str:
    return json.dumps({
        "summary": "Payment failures and slow support drove most complaints this week.",
        "themes": ["Payments fail while money is debited.", "Tickets go unanswered.",
                   "The app is slow to load."],
        "actions": ["Show payment status and refund timelines in the app.",
                    "Route stuck tickets to a human within a day.",
                    "Profile and fix the slow portfolio screen."],
    })  # fmt: skip


def explainer_handler(messages: Sequence[Mapping[str, str]]) -> str:
    return json.dumps({"bullets": [
        {"text": "An exit load is a charge when units are redeemed early.", "passage": 1},
        {"text": "HDFC Liquid Fund charges 0.0070% if redeemed on day 1.", "passage": 2},
        {"text": "No exit load applies from day 7 onwards.", "passage": 2},
    ]})  # fmt: skip


def chunk(chunk_id: str, key: str, url: str, text: str, similarity: float = 0.8) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, source_key=key, url=url, title=key, publisher="sebi",
        doc_type="faq", scheme="general", section=None, page=1, text=text,
        fetched_at=datetime(2026, 11, 17, tzinfo=UTC), similarity=similarity,
        text_rank=None, score=similarity,
    )  # fmt: skip


EXIT_LOAD_CHUNKS = [
    chunk("c1", "sebi_mf_faq", "https://www.sebi.gov.in/faq.pdf",
          "Exit load is a charge collected when an investor redeems units early."),
    chunk("c2", "hdfc_liquid_kim", "https://files.hdfcfund.com/kim-liquid.pdf",
          "Exit load: 0.0070% if redeemed on Day 1; Nil from Day 7 onwards.", 0.78),
    chunk("c3", "amfi_risk", "https://www.amfiindia.com/risk",
          "Investors should read the scheme documents carefully.", 0.70),
]  # fmt: skip


class FakeRetriever:
    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        query: str,
        *,
        scheme: str | None = None,
        include_platform: bool = False,
        k: int = 4,
        doc_types: Sequence[str] | None = None,
    ) -> tuple[list[RetrievedChunk], SearchTrace]:
        self.calls.append({"query": query, "doc_types": doc_types, "k": k})
        return self.chunks[:k], SearchTrace(query, scheme, include_platform)
