from __future__ import annotations

import re
from typing import Any

from .responder import manual_knowledge_allowed

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_LONG_ID_RE = re.compile(r"\b\d{6,}\b")

_PERSONAL_MARKERS = [
    "мой ребенок", "моего ребенка", "моему ребенку", "моя дочь", "мой сын", "у сына", "у дочери", "у ребенка",
    "наш ребенок", "наша дочь", "наш сын", "мою дочь", "моего сына", "фио", "паспорт", "снилс",
    "договор №", "заказ №", "оплата №", "карта", "cvc", "cvv", "смс", "sms",
]

_HIGH_RISK_OR_DYNAMIC = [
    "аллерг", "диагноз", "лекар", "медицин", "температур", "травм", "больниц", "скорую",
    "возврат", "вернуть деньги", "перенос смен", "оплат", "списал", "банк", "договор",
    "свободн", "есть места", "наличие мест", "бронь", "бронир", "беседк", "аренд",
]

_ONE_OFF = [
    "именно вам", "в вашем случае", "по вашей заявке", "по вашему ребенку", "по вашему ребёнку",
    "сегодня свобод", "сейчас свобод", "уточним", "уточню", "перезвоним", "свяжемся с вами", "ожидайте звонка",
]


def knowledge_candidate_allowed(ticket: Any, answer_text: str) -> tuple[bool, str]:
    """Conservative gate for manager answer -> reusable permanent knowledge.

    Nothing is ever saved automatically. This function only decides whether the manager may be
    offered Add/Edit/Skip buttons. Structured Smart Handoff tickets and any personal/high-risk/
    fast-changing content are excluded.
    """
    try:
        intake_type = str(ticket["intake_type"] or "")
    except Exception:
        intake_type = str(getattr(ticket, "intake_type", "") or "")
    if intake_type:
        return False, "structured_intake"

    try:
        question = str(ticket["question"] or "").strip()
    except Exception:
        question = str(getattr(ticket, "question", "") or "").strip()
    answer = (answer_text or "").strip()
    if len(question) < 5 or len(answer) < 5:
        return False, "too_short"
    if len(question) > 700 or len(answer) > 1500:
        return False, "too_long"

    combined = f"{question}\n{answer}"
    lower = combined.lower().replace("ё", "е")
    if _PHONE_RE.search(combined) or _EMAIL_RE.search(combined) or _LONG_ID_RE.search(combined):
        return False, "personal_or_contact_data"
    if any(marker in lower for marker in _PERSONAL_MARKERS):
        return False, "individual_case"
    if any(marker in lower for marker in _HIGH_RISK_OR_DYNAMIC):
        return False, "high_risk_or_dynamic"
    if any(marker in lower for marker in _ONE_OFF):
        return False, "one_off_answer"

    q_allowed, q_reason = manual_knowledge_allowed(question, kind="permanent")
    if not q_allowed:
        return False, q_reason
    a_allowed, a_reason = manual_knowledge_allowed(answer, kind="permanent")
    if not a_allowed:
        return False, a_reason
    return True, "ok"
