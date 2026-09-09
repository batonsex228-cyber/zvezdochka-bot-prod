from __future__ import annotations

import re
from dataclasses import dataclass


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е").strip())


@dataclass(frozen=True)
class IntentDecision:
    intent: str | None = None
    entity: str | None = None
    clarification: str | None = None
    confidence: float = 0.0


SHIFT_WORDS = {
    "1": 1, "1я": 1, "1-я": 1, "первая": 1, "первой": 1, "первую": 1,
    "2": 2, "2я": 2, "2-я": 2, "вторая": 2, "второй": 2, "вторую": 2,
    "3": 3, "3я": 3, "3-я": 3, "третья": 3, "третьей": 3, "третью": 3,
    "4": 4, "4я": 4, "4-я": 4, "четвертая": 4, "четвертой": 4, "четвертую": 4,
}


def _shift_number(n: str) -> int | None:
    # Explicit ordinals/numeric forms with or without the word "смена".
    for token, value in SHIFT_WORDS.items():
        if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", n):
            if "смен" in n or any(k in n for k in ["когда", "стоит", "цена", "начина", "заканч", "путев", "даты"]):
                return value
    m = re.search(r"\b([1-4])\s*[-–—]?\s*(?:я|ая)?\s*смен", n)
    return int(m.group(1)) if m else None


def _is_short_followup(n: str) -> bool:
    words = n.split()
    return len(words) <= 6 and (
        any(k in n for k in ["сколько", "стоит", "цена", "когда", "даты", "а когда", "а сколько"])
        or n in {"а цена?", "цена?", "а даты?", "а когда?", "сколько?"}
    )


def _looks_dynamic_service_or_capacity(n: str) -> bool:
    capacity = [
        "есть места", "остались места", "свободные места", "есть путевки", "есть путевка",
        "остались путевки", "осталась путевка", "не осталось ли путев", "путевки пока есть",
    ]
    if any(k in n for k in capacity):
        return True
    if "путев" in n and any(k in n for k in ["есть", "остал", "свобод", "налич"]):
        return True
    service = any(k in n for k in ["бесед", "корпус", "домик", "ночев", "мероприят"])
    availability = any(k in n for k in ["свободн", "занят", "доступн", "какие даты", "свободные даты", "свободное время"])
    return service and availability


def _dynamic_entity(n: str) -> str:
    if any(k in n for k in ["бесед", "корпус", "домик", "ночев", "мероприят"]):
        return "extra_service_dynamic"
    return "current_availability"


def _looks_booking_followup(n: str) -> bool:
    # A parent may ask about the service first and send the date as a second message: "А на 13 сентября?"
    if any(k in n for k in ["свобод", "занят", "доступ", "заброни", "бронь", "дата", "время"]):
        return True
    if any(k in n for k in ["сегодня", "завтра", "послезавтра", "суббот", "воскрес", "выходн"]):
        return True
    months = r"январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*"
    return bool(re.search(rf"\b\d{{1,2}}\s+(?:{months})\b|\b\d{{1,2}}[./-]\d{{1,2}}(?:[./-]\d{{2,4}})?\b", n))


def route(text: str, context: dict[str, str] | None = None) -> IntentDecision:
    n = norm(text)
    if not n:
        return IntentDecision()

    exact = {
        "📅 смены и цены": "shifts_prices", "🍲 питание": "food", "🎒 что взять": "bring",
        "📄 документы": "documents", "🚗 заезд": "arrival", "📞 контакты": "contacts",
        "🔔 важные уведомления": "notifications", "❓ задать свой вопрос": "ask",
    }
    if n in exact:
        return IntentDecision(exact[n], confidence=1.0)

    # Hard safety before broad lexical intents. These questions change too quickly to map to a cached answer.
    # If one message asks both arrival and exact visiting schedule, do not answer only half of it.
    if any(k in n for k in ["заезд", "привозить", "привезти"]) and any(k in n for k in ["навещ", "посещ"]):
        return IntentDecision(None, confidence=0.99)
    if any(k in n for k in ["навещ", "посещ"]) and any(k in n for k in ["в какие дни", "какие дни", "во сколько", "время посещ", "часы посещ"]):
        return IntentDecision(None, confidence=0.99)
    if _looks_dynamic_service_or_capacity(n):
        return IntentDecision(None, entity=_dynamic_entity(n), confidence=0.99)
    if "рассроч" in n or "кредит" in n:
        return IntentDecision(None, confidence=0.98)
    if ("скид" in n or "льгот" in n) and any(k in n for k in ["многодет", "пенсионер", "инвалид", "ветеран", "малоимущ", "сво"]):
        return IntentDecision(None, confidence=0.99)
    if any(k in n for k in ["новый год", "новогодн"]) and any(k in n for k in ["предлож", "программ", "выезд", "мероприят"]):
        return IntentDecision("new_year_offer", confidence=0.99)
    if any(k in n for k in ["класс", "групп", "команд"]) and any(k in n for k in ["ночев", "корпус"]) and any(k in n for k in ["что входит", "стоим", "сколько", "услови бронир", "предоплат", "аванс"]):
        return IntentDecision(None, confidence=0.99)

    # Preserve booking context even if the parent sends only a date in the next message.
    if context and (context.get("entity") == "extra_service_dynamic" or context.get("topic") == "extra_services") and _looks_booking_followup(n):
        return IntentDecision(None, entity="extra_service_dynamic", confidence=0.99)

    # Contextual follow-up: "Когда вторая смена?" -> "А сколько стоит?"
    if context and _is_short_followup(n):
        entity = context.get("entity") or ""
        if entity.startswith("shift:"):
            try:
                num = int(entity.split(":", 1)[1])
            except ValueError:
                num = 0
            if 1 <= num <= 4:
                return IntentDecision(f"shift_{num}", entity=entity, confidence=0.96)
        if context.get("topic") == "winter_shift":
            return IntentDecision("winter", entity="winter", confidence=0.95)
        if context.get("topic") == "extra_services" and any(k in n for k in ["сколько", "стоит", "цена"]):
            return IntentDecision("extra_services", confidence=0.92)

    if n in {"адрес", "какой адрес", "где вы", "где находитесь", "местоположение"}:
        return IntentDecision(clarification="address", confidence=1.0)

    # Live-dialogue intents: order matters (before generic documents/telephone/contact routing).
    if any(k in n for k in ["ндфл", "налоговый вычет", "возврат налога", "товарная накладная"]):
        return IntentDecision("ndfl_documents", confidence=0.98)
    if any(k in n for k in ["почт", "email", "e-mail", "емейл"]) and not "налог" in n:
        return IntentDecision("contacts", confidence=0.99)
    if (("забира" in n or "хран" in n or "выдают" in n or "выдать" in n) and "телефон" in n) or "телефоны у вожат" in n:
        return IntentDecision("phone_policy", confidence=0.99)
    if "договор" in n and any(k in n for k in ["документ", "паспорт", "оформ", "взять", "нужн"]):
        return IntentDecision("contract_documents", confidence=0.99)

    # Parents often write a shift number together with an arrival-time question. The object is still arrival,
    # not the published date/price card for that shift.
    if any(k in n for k in ["во сколько заезд", "время заезда", "к какому времени", "когда привозить", "во сколько привозить", "когда забирать", "во сколько выезд"]):
        return IntentDecision("arrival", confidence=0.99)

    shift = _shift_number(n)
    if shift:
        return IntentDecision(f"shift_{shift}", entity=f"shift:{shift}", confidence=0.98)
    if any(k in n for k in ["зимняя смен", "зимний лагерь", "северного сияния"]):
        return IntentDecision("winter", entity="winter", confidence=0.98)
    if any(k in n for k in ["смены и цены", "какие смены", "даты смен", "расписание смен", "сколько стоит путевка", "стоимость путевки", "цены на смен"]):
        return IntentDecision("shifts_prices", confidence=0.97)
    if any(k in n for k in ["скидк", "льгот"]):
        return IntentDecision("discounts", confidence=0.93)
    if any(k in n for k in ["079", "справк", "документ", "полис омс", "свидетельство о рождении"]):
        return IntentDecision("documents", confidence=0.96)

    if any(k in n for k in ["что нельзя", "нельзя брать", "запрещено", "запрещены", "запрещен"]):
        if any(k in n for k in ["ед", "продукт", "чипс", "газиров", "колбас", "сухар"]):
            return IntentDecision("prohibited_food", confidence=0.96)
        return IntentDecision("prohibited_items", confidence=0.93)
    if any(k in n for k in ["чипс", "газировка", "сухарики"]) and any(k in n for k in ["нельзя", "запрещ"]):
        return IntentDecision("prohibited_food", confidence=0.97)

    if any(k in n for k in ["что взять", "что брать", "что собрать", "вещи", "с собой"]):
        if "обув" in n: return IntentDecision("shoes", confidence=0.97)
        if any(k in n for k in ["гигиен", "зубн", "шампун", "мыло"]): return IntentDecision("hygiene", confidence=0.97)
        if any(k in n for k in ["одеж", "футбол", "штаны", "кофт"]): return IntentDecision("clothes", confidence=0.97)
        return IntentDecision("bring", confidence=0.94)
    if "обув" in n: return IntentDecision("shoes", confidence=0.95)
    if any(k in n for k in ["гигиен", "зубная щетка", "шампун"]): return IntentDecision("hygiene", confidence=0.95)
    if any(k in n for k in ["одежд", "футбол", "штаны", "свитер", "кофт"]): return IntentDecision("clothes", confidence=0.95)
    if any(k in n for k in ["питани", "кормят", "кормление", "сколько раз едят", "еда", "едят"]): return IntentDecision("food", confidence=0.94)
    if any(k in n for k in ["во сколько заезд", "когда заезд", "время заезда", "во сколько выезд", "когда выезд", "привозить ребенка", "забирать ребенка", "к какому времени завтра привозить", "во сколько завтра заезд"]):
        return IntentDecision("arrival", confidence=0.97)
    if "трансфер" in n: return IntentDecision("transfer", confidence=0.98)
    if any(k in n for k in ["офис", "куда ехать", "куда приехать", "где оформить", "где купить путев", "адрес продаж"]):
        return IntentDecision("contacts", entity="office", confidence=0.96)
    if any(k in n for k in ["телефон", "номер", "контакт", "почта", "email", "e-mail", "связаться"]): return IntentDecision("contacts", confidence=0.95)
    if any(k in n for k in ["возраст", "сколько лет", "с какого возраста", "до скольки лет"]): return IntentDecision("age", confidence=0.95)
    if any(k in n for k in ["навещ", "посещ", "приехать к ребенку"]): return IntentDecision("visits", confidence=0.9)
    if any(k in n for k in ["лекарств", "таблетк", "медикамент"]): return IntentDecision("medicines", confidence=0.89)
    if any(k in n for k in ["если заболел", "заболеет в лагере", "медпункт", "медицинский кабинет"]): return IntentDecision("illness", confidence=0.9)
    if any(k in n for k in ["территори", "что есть в лагере", "инфраструктур", "стадион", "площадк"]): return IntentDecision("territory", confidence=0.91)
    if any(k in n for k in ["квест", "беседк", "дополнительные услуги", "ночевк", "домик", "корпус", "выезд классом", "выезд группой"]):
        return IntentDecision("extra_services", confidence=0.92)
    if any(k in n for k in ["чем занимаются", "занимаются", "программа", "мероприятия для детей", "кружк", "робототех"]): return IntentDecision("program", confidence=0.9)
    if any(k in n for k in ["уведомлен", "рассылк"]): return IntentDecision("notifications", confidence=0.9)
    return IntentDecision()
