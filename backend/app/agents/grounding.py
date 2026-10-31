"""程序化 grounding：模型摘要里出现的编号和金额，必须能在本次工具结果里找到。

提示词已经要求模型不要编造工具返回之外的数据，但提示词不是边界。理财域早就有
cited_ids 白名单；这里把同一条规则提到公共层：领域 Agent 在把模型自己写的摘要交给
Supervisor 之前，先拿本次运行的证据核对一遍。没有工具证据的规则问答不参与校验，避免
把单笔超过 1,000 元需要验证码这类常识答句误判成幻觉。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

BLOCKED_SUMMARY = (
    "这次回答里的编号或金额无法与工具返回核对，已按安全策略拦截；"
    "请不要据此判断办理结果，可在账户流水或业务记录中核实。"
)

# Business identifiers the domains actually emit. A bare word such as card is not an
# identifier; the suffix must be long enough and contain a digit, so prose about cards
# or subscriptions does not trip the check.
_ID_PREFIXES = (
    "txn|task|ord|sub|prod|hold|appt|act|aud|evt|conv|msg|run|req|call|sess|plan|"
    "email|baud|pay|card"
)
_ID_PATTERN = re.compile(rf"\b(?:{_ID_PREFIXES})_[a-z0-9]{{5,}}\b", re.IGNORECASE)
_CARD_ACTION_PATTERN = re.compile(r"\bCA-[A-Za-z0-9]{8,}\b")
_MONEY_PATTERN = re.compile(
    r"(?:[¥￥]\s*(?P<prefix>[0-9][0-9,]*(?:\.[0-9]{1,2})?))"
    r"|(?P<suffix>[0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?P<unit>万元|万|元|块)"
)


@dataclass(frozen=True, slots=True)
class GroundingResult:
    """What to show instead, and why the original summary was rejected."""

    summary: str
    blocked: bool = False
    reason: str | None = None


def guard_summary(
    summary: str,
    *,
    thread: Sequence[Mapping[str, Any]] | None = None,
    facts: Mapping[str, Any] | None = None,
    business_ids: Mapping[str, Any] | None = None,
    user_text: str | None = None,
) -> GroundingResult:
    """Verify the model-authored summary against the evidence this activation collected.

    thread is the model-visible activation thread: tool envelopes carry result, the
    only place tool output reaches the model. facts and business_ids are the structured
    values the tools returned alongside those summaries. A summary is only checked when at
    least one tool result (or structured fact) exists; otherwise the domain is answering a
    rule question and there is nothing to ground against.
    """

    tool_texts = _tool_result_texts(thread or ())
    if not tool_texts and not facts and not business_ids:
        return GroundingResult(summary)

    evidence = [*tool_texts, *_user_texts(thread or ())]
    if user_text:
        evidence.append(user_text)
    if facts:
        evidence.append(_facts_text(facts))
    if business_ids:
        evidence.append(" ".join(str(value) for value in business_ids.values()))

    joined = "\n".join(evidence)
    evidence_ids = _identifiers(joined)
    for identifier in _identifiers(summary):
        if identifier.lower() not in evidence_ids:
            return GroundingResult(
                BLOCKED_SUMMARY,
                blocked=True,
                reason=f"摘要引用了工具没有返回过的编号 {identifier}",
            )

    evidence_amounts = set(_amounts(joined).values())
    for raw, amount in _amounts(summary).items():
        if amount not in evidence_amounts:
            return GroundingResult(
                BLOCKED_SUMMARY,
                blocked=True,
                reason=f"摘要引用了工具没有返回过的金额 {raw}",
            )
    return GroundingResult(summary)


def _tool_result_texts(thread: Sequence[Mapping[str, Any]]) -> list[str]:
    texts: list[str] = []
    for message in thread:
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, Mapping) or "tool" not in payload or "result" not in payload:
            continue
        result = payload["result"]
        texts.append(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
    return texts


def _user_texts(thread: Sequence[Mapping[str, Any]]) -> list[str]:
    """User-authored lines already in the activation thread count as evidence."""

    texts: list[str] = []
    for message in thread:
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, Mapping) and "tool" in payload and "result" in payload:
            continue
        texts.append(content)
    return texts


def _facts_text(facts: Mapping[str, Any]) -> str:
    parts = [json.dumps(facts, ensure_ascii=False, default=str)]
    for key, value in facts.items():
        if key.endswith("_minor") and isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(f"{key} = {Decimal(str(value)) / 100:.2f} 元")
    return "\n".join(parts)


def _identifiers(text: str) -> set[str]:
    found = {match.group(0).lower() for match in _ID_PATTERN.finditer(text)}
    found.update(match.group(0).lower() for match in _CARD_ACTION_PATTERN.finditer(text))
    # A prefix with no digit is a word (card_expiry), not an identifier this check owns.
    return {item for item in found if any(char.isdigit() for char in item)}


def _amounts(text: str) -> dict[str, Decimal]:
    amounts: dict[str, Decimal] = {}
    for match in _MONEY_PATTERN.finditer(text):
        raw = match.group("prefix") or match.group("suffix")
        if not raw:
            continue
        try:
            value = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        unit = match.group("unit")
        if unit in ("万元", "万"):
            value *= 10000
        amounts[raw] = value
    return amounts
