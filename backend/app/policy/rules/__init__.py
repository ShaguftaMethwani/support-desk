"""Loads the YAML rule files once and compiles their patterns."""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern[str]
    scope: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AdviceRules:
    block: tuple[Rule, ...]
    allow: tuple[Rule, ...]


@dataclass(frozen=True)
class ReturnsRules:
    window_chars: int
    return_words: re.Pattern[str]
    fee_words: re.Pattern[str]
    mask: re.Pattern[str]
    claims: tuple[Rule, ...]


def _load(name: str) -> dict[str, Any]:
    with open(_DIR / name, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern.strip(), re.IGNORECASE | re.MULTILINE)


def _words(words: list[str]) -> re.Pattern[str]:
    return _compile(r"\b(?:" + "|".join(words) + r")\b")


@lru_cache(maxsize=1)
def advice_rules() -> AdviceRules:
    data = _load("advice.yaml")
    block = tuple(
        Rule(r["id"], _compile(r["pattern"]), frozenset(r.get("scope", []))) for r in data["block"]
    )
    allow = tuple(Rule(r["id"], _compile(r["pattern"])) for r in data.get("allow", []))
    return AdviceRules(block, allow)


@lru_cache(maxsize=1)
def returns_rules() -> ReturnsRules:
    data = _load("returns.yaml")
    return ReturnsRules(
        window_chars=int(data["window_chars"]),
        return_words=_words(data["return_words"]),
        fee_words=_words(data["fee_words"]),
        mask=_compile("|".join(f"(?:{p})" for p in data["mask_phrases"])),
        claims=tuple(Rule(c["id"], _compile(c["pattern"])) for c in data["claims"]),
    )
