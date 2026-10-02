from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .db import Database


@dataclass(frozen=True)
class IntakeUser:
    user_id: int
    peer_id: int
    full_name: str
    username: str | None = None


@dataclass(frozen=True)
class IntakeResult:
    handled: bool
    text: str | None = None
    state: str = ""
    needs_parent_confirmation: bool = False
    submission: dict[str, Any] | None = None
    error_reason: str | None = None
    intake_type: str | None = None


@dataclass(frozen=True)
class Step:
    field: str
    label: str
    prompt: str
    kind: str = "text"
    optional: bool = False


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    steps: tuple[Step, ...]
    priority: str = "normal"
    intro: str | None = None


PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
CARD_RE = re.compile(r"(?<!\d)(?:\d[\s\-]*){16,19}(?!\d)")
SMS_CODE_RE = re.compile(r"(?i)\b(?:код|sms|смс|cvc|cvv)\b.{0,20}\b\d{3,8}\b")

SHIFT_WORDS = {
    "первая": "1 смена", "первую": "1 смена", "первой": "1 смена", "первый": "1 смена",
    "вторая": "2 смена", "вторую": "2 смена", "второй": "2 смена",
    "третья": "3 смена", "третью": "3 смена", "третьей": "3 смена", "третий": "3 смена",
    "четвертая": "4 смена", "четвертую": "4 смена", "четвертой": "4 смена", "четвертый": "4 смена",
    "пятая": "5 смена", "пятую": "5 смена", "пятой": "5 смена", "пятый": "5 смена",
    "шестая": "6 смена", "шестую": "6 смена", "шестой": "6 смена", "шестой": "6 смена",
}


def _norm(text: str) -> str:
    return " ".join((text or "").lower().replace("ё", "е").split())


def _normalize_phone(text: str) -> str | None:
    match = PHONE_RE.search(text or "")
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) != 11:
        return None
    if digits.startswith("8"):
        digits = "7" + digits[1:]
    if not digits.startswith("7"):
        return None
    return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def _payment_secret(text: str) -> bool:
    raw = text or ""
    return bool(CARD_RE.search(raw) or SMS_CODE_RE.search(raw))


def _parse_name(text: str) -> str | None:
    value = " ".join((text or "").strip().split()).strip(".,;:")
    if not 5 <= len(value) <= 90:
        return None
    words = value.split()
    if not 2 <= len(words) <= 4:
        return None
    if not all(re.fullmatch(r"[А-Яа-яЁёA-Za-z-]{2,}", word) for word in words):
        return None
    return value


def _labelled_value(text: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        m = re.search(rf"(?:{label})\s*[:=-]\s*([^,;\n]{{5,90}})", text or "", re.IGNORECASE)
        if m:
            return m.group(1).strip(" .")
    return None


def _shift_mentions(text: str) -> list[str]:
    n = _norm(text)
    out: list[tuple[int, str]] = []
    if "зимн" in n:
        out.append((n.index("зимн"), "зимняя смена"))
    for m in re.finditer(r"(?<!\d)([1-9])\s*[-–—]?\s*(?:я|й|ую|ой)?\s*смен", n):
        out.append((m.start(), f"{m.group(1)} смена"))
    for word, value in SHIFT_WORDS.items():
        for m in re.finditer(rf"\b{re.escape(word)}\b(?:\s+смен\w*)?", n):
            out.append((m.start(), value))
    out.sort(key=lambda x: x[0])
    result: list[str] = []
    for _, value in out:
        if not result or result[-1] != value:
            result.append(value)
    return result


def _parse_shift(text: str) -> str | None:
    values = _shift_mentions(text)
    return values[0] if values else None


def _parse_age(text: str) -> str | None:
    m = re.search(r"(?<!\d)(\d{1,2})\s*(?:лет|года?|год)\b", _norm(text))
    if not m:
        if re.fullmatch(r"\s*\d{1,2}\s*", text or ""):
            m = re.search(r"\d{1,2}", text)
        else:
            return None
    age = int(m.group(1) if m.lastindex else m.group(0))
    if not 1 <= age <= 20:
        return None
    return str(age)


def _question_like(text: str) -> bool:
    n = _norm(text)
    if "?" in (text or ""):
        return True
    starters = (
        "когда ", "сколько ", "какие ", "какой ", "какая ", "где ", "как ", "можно ли ",
        "есть ли ", "во сколько ", "что взять", "что нужно", "подскажите когда", "подскажите сколько",
    )
    if n.startswith(starters):
        return True
    # VK users often send a short menu-like question without a question mark. Never consume
    # those as a free-text field in an active form.
    if len(n) <= 80 and any(x in n for x in [
        "документ", "питани", "что взять", "цена", "стоимость", "смены", "адрес",
        "заезд", "выезд", "контакт", "телефон лагер", "распорядок", "кружк",
    ]):
        return True
    return False


def _yes(text: str) -> bool:
    n = _norm(text)
    return n in {"да", "верно", "все верно", "подтверждаю", "передать", "отправить", "ок", "хорошо"}


def _cancel(text: str) -> bool:
    n = _norm(text)
    return n in {"отмена", "отменить", "не надо", "не нужно", "стоп", "передумал", "передумала"}


def _status(text: str) -> bool:
    n = _norm(text)
    return any(x in n for x in ["что с заявкой", "статус заявки", "моя заявка", "заявка отправлена"])


def _clean_free_text(text: str, *, max_len: int = 350) -> str | None:
    value = " ".join((text or "").strip().split())
    if len(value) < 2 or len(value) > max_len:
        return None
    if _payment_secret(value):
        return None
    return value


SCENARIOS: dict[str, Scenario] = {
    "refund": Scenario(
        "refund", "Возврат / отмена путёвки",
        (
            Step("shift", "Смена", "На какую смену оформлена путёвка? Например: «2 смена». ", "shift"),
            Step("child_name", "Ребёнок", "Напишите, пожалуйста, ФИО ребёнка.", "name"),
            Step("payer_name", "Плательщик", "Напишите ФИО человека, на которого оформлена оплата/договор.", "name"),
            Step("phone", "Телефон", "Оставьте номер телефона для связи в формате +7 …", "phone"),
            Step("reason", "Причина", "Коротко укажите причину возврата одним предложением. Если причина медицинская — без диагноза и лишних подробностей."),
        ),
    ),
    "transfer": Scenario(
        "transfer", "Перенос на другую смену",
        (
            Step("current_shift", "Текущая смена", "С какой смены хотите перенести ребёнка?", "shift"),
            Step("desired_shift", "Желаемая смена", "На какую смену хотите перенести?", "shift"),
            Step("child_name", "Ребёнок", "Напишите ФИО ребёнка.", "name"),
            Step("phone", "Телефон", "Оставьте номер телефона для связи в формате +7 …", "phone"),
        ),
    ),
    "payment": Scenario(
        "payment", "Проблема с оплатой",
        (
            Step("payment_context", "Что произошло", "Коротко опишите, что произошло с оплатой: не проходит, списалось дважды, не пришло подтверждение и т. п."),
            Step("shift", "Смена", "К какой смене относится оплата?", "shift"),
            Step("phone", "Телефон", "Оставьте номер телефона для связи в формате +7 …", "phone"),
        ),
        intro="🔐 Не присылайте номер банковской карты, CVC/CVV и коды из SMS. Для заявки они не нужны.",
    ),
    "lost_item": Scenario(
        "lost_item", "Потерянная вещь",
        (
            Step("shift", "Смена", "На какой смене был ребёнок?", "shift"),
            Step("child_name", "Ребёнок", "Напишите ФИО ребёнка.", "name"),
            Step("item", "Что потеряно", "Что именно потерялось? Например: «чёрная толстовка Adidas, размер 152»."),
            Step("details", "Отличительные признаки", "Есть ли отличительные признаки: подпись, цвет, размер, рисунок? Если нет — напишите «нет».", optional=True),
        ),
    ),
    "together": Scenario(
        "together", "Просьба поселить / определить вместе",
        (
            Step("shift", "Смена", "На какую смену едут дети?", "shift"),
            Step("children", "Дети", "Напишите ФИО детей, которых хотите определить вместе."),
            Step("preference", "Пожелание", "Что именно важно: один отряд, одна комната или оба варианта?"),
        ),
    ),
    "food": Scenario(
        "food", "Индивидуальный вопрос по питанию",
        (
            Step("shift", "Смена", "На какую смену едет ребёнок?", "shift"),
            Step("child_name", "Ребёнок", "Напишите ФИО ребёнка.", "name"),
            Step("phone", "Телефон", "Оставьте номер телефона для связи в формате +7 …", "phone"),
        ),
        intro="🥗 Индивидуальные ограничения по питанию должен подтвердить сотрудник лагеря. Я соберу только данные для связи; медицинские подробности здесь повторять не нужно.",
    ),
    "medicine": Scenario(
        "medicine", "Лекарства / индивидуальная медицинская ситуация",
        (
            Step("shift", "Смена", "На какую смену едет ребёнок?", "shift"),
            Step("child_name", "Ребёнок", "Напишите ФИО ребёнка.", "name"),
            Step("phone", "Телефон", "Оставьте номер телефона для связи в формате +7 …", "phone"),
        ),
        intro="🩺 Индивидуальные решения по лекарствам и состоянию здоровья принимает сотрудник лагеря. Я не буду давать медицинских рекомендаций — только соберу данные для связи.",
    ),
    "contact_child": Scenario(
        "contact_child", "Связаться с ребёнком",
        (
            Step("shift", "Смена", "На какой смене сейчас ребёнок?", "shift"),
            Step("child_name", "Ребёнок", "Напишите ФИО ребёнка.", "name"),
            Step("phone", "Телефон родителя", "Оставьте номер телефона, по которому с Вами можно связаться.", "phone"),
            Step("reason", "Причина", "Коротко напишите причину связи одним предложением."),
        ),
        priority="high",
    ),
    "complaint": Scenario(
        "complaint", "Жалоба / конфликт / ситуация в отряде",
        (
            Step("shift", "Смена", "На какой смене находится ребёнок?", "shift"),
            Step("child_name", "Ребёнок", "Напишите ФИО ребёнка.", "name"),
            Step("summary", "Суть ситуации", "Коротко опишите ситуацию без лишних персональных данных других детей."),
            Step("phone", "Телефон", "Оставьте номер телефона для связи в формате +7 …", "phone"),
        ),
        priority="high",
    ),
    "shift_availability": Scenario(
        "shift_availability", "Проверка мест на смену",
        (
            Step("shift", "Смена", "Какая смена Вас интересует?", "shift"),
            Step("child_age", "Возраст ребёнка", "Сколько лет ребёнку?", "age"),
            Step("phone", "Телефон", "Оставьте номер телефона для связи в формате +7 …", "phone"),
        ),
    ),
}


URGENT_PATTERNS = [
    r"\bне\s+могу\s+найти\s+(?:ребенка|сына|дочь)",
    r"\b(?:ребенок|сын|дочь)\s+пропал",
    r"\bбез\s+сознания\b",
    r"\bне\s+дышит\b",
    r"\bсильн\w*\s+кровотеч",
    r"\bугроз\w*\s+(?:жизни|здоровью)",
    r"\b(?:ребенок|сын|дочь)\s+(?:получил|получила)\s+(?:серьезн\w*\s+)?травм",
    r"\bсрочн\w*\s+медицин\w*\s+помощ",
]


def _urgent(text: str) -> bool:
    n = _norm(text)
    if any(re.search(p, n) for p in URGENT_PATTERNS):
        return True

    # Fire/violence words also appear in ordinary policy questions (e.g.
    # «Как обеспечена пожарная безопасность?»). Treat them as urgent only when the
    # message describes an incident, not merely the topic.
    fire_plain = n.strip(" .,!?:;-") == "пожар"
    fire_incident = any(x in n for x in [
        "у нас пожар", "сейчас пожар", "начался пожар", "начался пожар",
        "горит корпус", "горит комната", "горит здание", "что-то горит",
        "загорелся", "загорелась", "загорелось", "возгорание сейчас",
    ])
    if fire_plain or fire_incident:
        return True

    violence_incident = (
        any(x in n for x in ["насилие над ребен", "насилие над сын", "насилие над доч", "совершили насили", "произошло насили", "случилось насили"])
        or ("насили" in n and any(x in n for x in ["сейчас", "только что", "произош", "случил", "над моим", "над нашим"]))
    )
    return violence_incident


def is_urgent_safety(text: str) -> bool:
    """Public safety predicate used by the adapter to bypass any active form/booking."""
    return _urgent(text)


def contains_payment_secret(text: str) -> bool:
    """Public guard used before any FAQ/booking/handoff persistence."""
    return _payment_secret(text)


def redact_payment_secrets(text: str) -> str:
    """Redact obvious card/CVC/SMS secrets before an urgent message is persisted or forwarded."""
    value = CARD_RE.sub("[номер карты скрыт]", text or "")
    value = SMS_CODE_RE.sub("[платёжный код скрыт]", value)
    return value


def build_urgent_submission(text: str) -> dict[str, Any]:
    """Build a redacted urgent ticket independently of whether Smart Handoff is enabled."""
    safe_text = redact_payment_secrets(text).strip()
    return {
        "intake_type": "urgent_safety",
        "title": "Срочная ситуация",
        "priority": "urgent",
        "request_id": uuid.uuid4().hex,
        "fields": {},
        "display_fields": [],
        "original_question": safe_text,
    }


def classify_transaction(text: str) -> str | None:
    n = _norm(text)
    if not n:
        return None
    if _urgent(n):
        return "urgent_safety"

    # Dynamic capacity is never safe to answer from a cached FAQ. Accept common Russian word order
    # variants: «есть места», «места есть», «остались путёвки», «путёвка ещё есть».
    availability_phrase = bool(
        "есть ли места" in n or "есть ли путев" in n
        or re.search(r"\b(?:есть|остал\w*|свободн\w*)\s+(?:мест\w*|путев\w*)\b", n)
        or re.search(r"\b(?:мест\w*|путев\w*)\s+(?:есть|остал\w*|свободн\w*)\b", n)
        or "наличие мест" in n
    )
    if availability_phrase and ("смен" in n or "путев" in n):
        return "shift_availability"

    # Current operational failures are actual cases even when phrased as a question.
    payment_failure = any(x in n for x in [
        "не проходит оплат", "оплата не проходит", "ошибка оплат", "ошибка при оплат",
        "не могу оплат", "деньги списали", "деньги списались", "списалось дважды",
        "списали два", "двойное списание", "подтверждение не пришло", "не пришло подтверждение",
        "платеж отклон", "платёж отклон", "путевки нет", "путевка не появилась",
    ])
    if payment_failure:
        return "payment"

    # Direct trouble contacting a child is an operational case, not the generic «как связаться?» FAQ.
    if any(x in n for x in ["не могу дозвон", "не дозвониться", "не выходит на связь", "не отвечает на звон", "не отвечает на телефон"]):
        return "contact_child"
    if "сроч" in n and any(x in n for x in ["связаться с ребен", "связаться с сын", "связаться с доч", "дозвон"]):
        return "contact_child"

    # Concrete conflict/complaint reports should be structured before the generic policy guard.
    personal_complaint_marker = any(x in n for x in [
        "у ребенка", "у сына", "у доч", "моего ребенка", "мою дочь", "моего сына",
        "ребенка обижа", "сына обижа", "дочь обижа", "ребенка трав", "сына трав", "дочь трав",
    ])
    if (
        any(x in n for x in [
            "травят ребенка", "обижают ребенка", "издеваются над ребен", "хочу пожаловаться",
            "жалоба на вожат", "проблема с вожат",
        ])
        or ("буллинг" in n and (personal_complaint_marker or any(x in n for x in ["хочу пожаловаться", "хочу сообщить", "у нас"])))
        or (any(x in n for x in ["конфликт в отряде", "конфликт с вожат"]) and personal_complaint_marker)
        or re.search(r"\b(?:ребенка|ребенок|сына|сын|дочь|дочку)\b.{0,45}\b(?:обижают|травят|буллят|издеваются|конфликт)\b", n)
        or re.search(r"\b(?:конфликт|проблема)\b.{0,35}\b(?:с детьми|с ребенком|с вожат)\b", n)
    ):
        return "complaint"

    # Some first-person requests are clearly actionable even though they start with «можно»/«как».
    actual_refund = (
        any(x in n for x in ["ребенок заболел", "ребенок заболела", "ребенок не поедет", "ребенок не сможет поехать"])
        and any(x in n for x in ["возврат", "отмен", "вернуть"])
    )
    if actual_refund:
        return "refund"

    # Generic policy/how-it-works questions belong to the normal FAQ/knowledge router. Smart Handoff
    # starts only when the parent is actually trying to do something.
    informational_prefixes = (
        "можно ли ", "какие условия ", "как происходит ", "как работает ", "что нужно для ",
        "сколько ", "как ", "где ", "куда ", "когда ", "кому ", "нужно ли ",
        "разрешено ли ", "во сколько ", "что делать если ", "что делать, если ",
    )
    explicit_action = any(x in n for x in [
        "хочу ", "хотим ", "мне нужно ", "нам нужно ", "прошу ", "нам надо ", "мне надо ",
        "помогите ", "пожалуйста, перенес", "пожалуйста перенес", "нужно согласовать",
        "хочу обсудить", "хочу сообщить", "хочу предупредить", "можно нас ", "можно попросить ",
    ])
    if n.startswith(informational_prefixes) and not explicit_action:
        return None

    # Refund / transfer requests.
    if ("путев" in n or "смен" in n) and any(x in n for x in [
        "хочу вернуть", "хотим вернуть", "нужно вернуть", "оформить возврат", "отменить путев",
        "ребенок не поедет", "ребенок не сможет поехать", "отказаться от смены",
    ]):
        return "refund"
    if (
        any(x in n for x in ["перенести ребенка", "перенести путев", "перенести на другую смен", "поменять смен", "сменить смену", "с одной смены на другую"])
        or (("поменять" in n or "перенести" in n) and "смен" in n)
        or ("сменить дату заезда" in n and "смен" in n)
        or ("можно нас перенести" in n and ("смен" in n or len(_shift_mentions(text)) >= 2))
    ):
        return "transfer"

    # Payment failures not caught by the high-priority block above.
    if any(x in n for x in [
        "не проходит оплат", "оплата не проходит", "ошибка оплат", "ошибка при оплат", "не могу оплат",
        "деньги списали", "деньги списались", "списалось дважды", "двойное списание",
        "подтверждение не пришло", "не пришло подтверждение", "платеж отклон", "платёж отклон",
    ]):
        return "payment"

    # Lost property. At this point generic «что делать, если ...?» policy questions have already
    # returned to the FAQ router, so these are concrete cases.
    if re.search(r"\b(?:ребенок|сын|дочь|дочка|мы|я)\b.{0,55}\b(?:потерял|потеряла|потеряли|забыл|забыла|забыли|оставил|оставила|оставили)\b", n):
        return "lost_item"
    if any(x in n for x in ["помогите найти", "не можем найти", "не могу найти"]) and any(
        x in n for x in ["вещ", "куртк", "кофт", "обув", "кроссов", "телефон", "сумк", "рюкзак", "одежд", "после лагер", "в лагере"]
    ):
        return "lost_item"
    if re.search(r"\b(?:остал\w*|забыт\w*)\b.{0,35}\b(?:в лагере|в корпусе|после смены)\b", n):
        return "lost_item"
    if any(x in n for x in ["потерял", "потеряла", "забыли вещ", "забыл вещ", "забыла вещ", "оставили вещ", "оставил вещ", "оставила вещ", "потеряш"]):
        return "lost_item"

    # Friends/siblings together: require a placement context so «хотим быть вместе на празднике» is not captured.
    placement = any(x in n for x in ["отряд", "комнат", "посел", "определ", "размест"])
    people_context = any(x in n for x in ["дет", "друг", "подруг", "брат", "сестр", "ребен"])
    if placement and any(x in n for x in ["вместе", "один отряд", "одном отряде", "одной комнате", "одну комнату"]):
        if (people_context and explicit_action) or any(x in n for x in ["поселите", "определите"]) or (explicit_action and "отряд" in n):
            return "together"

    # Individual food restrictions. Generic «как сообщить об аллергии?» remains FAQ/human guidance.
    if any(x in n for x in ["аллерги", "непереносимость", "особое питание", "медицинской диет", "диета по медицин"]):
        if any(x in n for x in ["у ребенка", "у сына", "у доч", "моему ребенку", "ребенок на", "сын на", "дочь на", "нужно особое питание", "хочу предупредить"]):
            return "food"

    # General medicine procedure questions should stay in the approved FAQ.
    med_policy_phrases = [
        "как передать лекар", "как их передать", "как их передавать", "кому передать лекар",
        "куда передать лекар", "можно ли передать лекар", "как передавать лекар",
    ]
    if any(x in n for x in med_policy_phrases) and not explicit_action:
        return None
    medical_marker = any(x in n for x in ["лекар", "таблет", "ингалятор", "астм", "диабет", "эпилеп", "укол", "инсулин"])
    medical_case = any(x in n for x in [
        "ребенок принимает", "сын принимает", "дочь принимает", "нужно принимать", "строго по времени",
        "нужно согласовать", "хочу обсудить", "хочу связаться с мед", "сыну нужен", "дочери нужен", "ребенку нужен",
    ])
    if medical_marker and (explicit_action or medical_case):
        return "medicine"

    # Contact child / request a callback from the child.
    if any(x in n for x in [
        "связаться с ребен", "связаться с сын", "связаться с доч", "передайте ребенку", "передайте сын", "передайте доч",
        "не могу дозвон", "не дозвониться", "не выходит на связь",
    ]) and (explicit_action or "сроч" in n or any(x in n for x in ["не могу", "не выходит", "передайте"])):
        return "contact_child"

    # Late complaint fallback for short actionable messages. Keep generic policy questions
    # (e.g. «Как лагерь борется с буллингом?») out of Smart Handoff.
    if any(x in n for x in ["хочу пожаловаться", "жалоба на", "травят ребенка", "обижают ребенка", "проблема с вожат"]):
        return "complaint"
    if "буллинг" in n and (personal_complaint_marker or explicit_action):
        return "complaint"
    if any(x in n for x in ["конфликт в отряде", "конфликт с вожат"]) and (personal_complaint_marker or explicit_action):
        return "complaint"

    return None


class SmartIntakeEngine:
    """Deterministic pre-handoff questionnaire. No LLM decisions are used here."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db

    def _scenario(self, key: str) -> Scenario:
        return SCENARIOS[key]

    def has_active(self, user_id: int) -> bool:
        return self.db.get_intake_session(user_id=user_id) is not None

    def abandon_for_other_flow(self, user_id: int) -> None:
        """Clear an unfinished intake when the parent explicitly switches to Booking."""
        self.db.clear_intake_session(user_id=user_id)

    def candidate(self, text: str, *, user_id: int) -> bool:
        if _urgent(text):
            return True
        row = self.db.get_intake_session(user_id=user_id)
        if not row:
            return classify_transaction(text) is not None
        if _cancel(text) or _status(text) or _yes(text):
            return True
        status = str(row.get("status") or "")
        if status == "confirm_parent":
            return False
        key = str(row.get("intake_type") or "")
        if key not in SCENARIOS:
            return False
        step_name = str(row.get("step") or "")
        step = next((s for s in self._scenario(key).steps if s.field == step_name), None)
        if step is None:
            return False
        # An unrelated support question must go through the normal FAQ/router and must not
        # be swallowed merely because an intake session exists. The session stays saved.
        if _question_like(text):
            return False
        return True

    def _parse(self, step: Step, text: str) -> tuple[bool, str | None, str | None]:
        if _payment_secret(text):
            return False, None, "🔐 Не присылайте номер карты, CVC/CVV или код из SMS. Эти данные боту и менеджеру не нужны."
        if step.kind == "phone":
            value = _normalize_phone(text)
            return (True, value, None) if value else (False, None, "Не получилось распознать номер. Напишите, пожалуйста, российский номер в формате +7 912 345-67-89.")
        if step.kind == "shift":
            value = _parse_shift(text)
            return (True, value, None) if value else (False, None, "Не понял номер смены. Напишите, например: «2 смена» или «зимняя смена».")
        if step.kind == "age":
            value = _parse_age(text)
            return (True, value, None) if value else (False, None, "Напишите возраст ребёнка числом, например: «10 лет».")
        if step.kind == "name":
            value = _parse_name(text)
            return (True, value, None) if value else (False, None, "Напишите, пожалуйста, имя и фамилию ребёнка/плательщика, например: «Иванов Иван».")
        value = _clean_free_text(text)
        if value is None:
            return False, None, "Напишите, пожалуйста, коротко одним сообщением."
        if step.optional and _norm(value) in {"нет", "не знаю", "без особенностей", "нет особенностей"}:
            value = "—"
        return True, value, None

    def _initial_payload(self, scenario: Scenario, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": scenario.title,
            "priority": scenario.priority,
            "request_id": uuid.uuid4().hex,
            "fields": {},
            "original_question": text.strip(),
        }
        fields = payload["fields"]
        phone = _normalize_phone(text)
        if phone:
            fields["phone"] = phone
        mentions = _shift_mentions(text)
        if scenario.key == "transfer" and mentions:
            fields["current_shift"] = mentions[0]
            if len(mentions) > 1:
                fields["desired_shift"] = mentions[1]
        elif mentions:
            fields["shift"] = mentions[0]
        age = _parse_age(text)
        if age and any(s.field == "child_age" for s in scenario.steps):
            fields["child_age"] = age
        child = _labelled_value(text, (r"фио\s+ребенка", r"ребенок"))
        if child and _parse_name(child) and any(s.field == "child_name" for s in scenario.steps):
            fields["child_name"] = _parse_name(child)
        payer = _labelled_value(text, (r"фио\s+плательщика", r"плательщик"))
        if payer and _parse_name(payer) and any(s.field == "payer_name" for s in scenario.steps):
            fields["payer_name"] = _parse_name(payer)

        # Reuse information that the parent has already clearly provided instead of asking twice.
        n = _norm(text)
        if scenario.key == "payment" and len(text.strip()) >= 10:
            fields["payment_context"] = text.strip()[:350]
        if scenario.key == "refund":
            reason_markers = ["заболел", "заболела", "не сможет поехать", "не поедет", "семейн", "отмен"]
            if any(x in n for x in reason_markers):
                fields["reason"] = text.strip()[:350]
        if scenario.key == "together":
            if "один отряд" in n or "в один отряд" in n:
                fields["preference"] = "Один отряд"
            elif "одной комнате" in n or "в одной комнате" in n:
                fields["preference"] = "Одна комната"
            elif "отряд" in n and "комнат" in n:
                fields["preference"] = "Один отряд и одна комната"
        return payload

    @staticmethod
    def _next_step(scenario: Scenario, fields: dict[str, Any]) -> Step | None:
        for step in scenario.steps:
            if not str(fields.get(step.field) or "").strip():
                return step
        return None

    @staticmethod
    def _summary(scenario: Scenario, fields: dict[str, Any]) -> str:
        lines = [f"📋 Проверьте заявку — {scenario.title}"]
        for step in scenario.steps:
            value = str(fields.get(step.field) or "—")
            lines.append(f"{step.label}: {value}")
        lines.append("\nВсё верно? После подтверждения я передам заявку специалисту.")
        return "\n".join(lines)

    @staticmethod
    def _submission(scenario: Scenario, payload: dict[str, Any]) -> dict[str, Any]:
        fields = dict(payload.get("fields") or {})
        display_fields = [
            {"key": step.field, "label": step.label, "value": str(fields.get(step.field) or "—")}
            for step in scenario.steps
        ]
        return {
            "intake_type": scenario.key,
            "title": scenario.title,
            "priority": str(payload.get("priority") or scenario.priority),
            "request_id": str(payload.get("request_id") or ""),
            "fields": fields,
            "display_fields": display_fields,
            "original_question": str(payload.get("original_question") or ""),
        }

    def _save(self, user: IntakeUser, scenario: Scenario, step: str, payload: dict[str, Any], *, status: str = "active") -> None:
        self.db.save_intake_session(
            user_id=user.user_id,
            peer_id=user.peer_id,
            intake_type=scenario.key,
            step=step,
            status=status,
            payload=payload,
            original_question=str(payload.get("original_question") or ""),
            ttl_minutes=self.settings.smart_handoff_ttl_minutes,
        )

    async def handle_text(self, user: IntakeUser, text: str) -> IntakeResult:
        row = self.db.get_intake_session(user_id=user.user_id)
        key = classify_transaction(text)

        # Safety always wins over an unfinished form. Do not let a request for a phone/name swallow
        # a new emergency message. Redact obvious payment secrets before persistence/forwarding.
        if key == "urgent_safety":
            if row is not None:
                self.db.clear_intake_session(user_id=user.user_id)
            submission = build_urgent_submission(text)
            return IntakeResult(
                True,
                "🔴 Я сразу передам это сообщение специалисту как срочное. Если есть непосредственная угроза жизни или здоровью — одновременно звоните 112.",
                state="submit_immediate",
                submission=submission,
            )

        if row is None:
            # Never persist card/CVC/SMS secrets, including when they are present in the very first message.
            if _payment_secret(text):
                return IntakeResult(
                    True,
                    "🔐 Не присылайте номер банковской карты, CVC/CVV или коды из SMS. Удалите эти данные и опишите только саму проблему с оплатой.",
                    state="security_reject",
                )
            if key is None:
                return IntakeResult(False)
            scenario = self._scenario(key)
            payload = self._initial_payload(scenario, text)
            step = self._next_step(scenario, payload["fields"])
            if step is None:
                self._save(user, scenario, "confirm_parent", payload, status="confirm_parent")
                intro = (scenario.intro + "\n\n") if scenario.intro else ""
                return IntakeResult(True, intro + self._summary(scenario, payload["fields"]), "confirm_parent", True)
            self._save(user, scenario, step.field, payload)
            intro = (scenario.intro + "\n\n") if scenario.intro else ""
            return IntakeResult(True, intro + step.prompt, "collecting")

        key = str(row.get("intake_type") or "")
        if key not in SCENARIOS:
            self.db.clear_intake_session(user_id=user.user_id)
            return IntakeResult(False)
        scenario = self._scenario(key)
        payload = dict(row.get("payload") or {})
        fields = dict(payload.get("fields") or {})
        payload["fields"] = fields
        status = str(row.get("status") or "")
        step_name = str(row.get("step") or "")

        if _cancel(text):
            self.db.clear_intake_session(user_id=user.user_id)
            return IntakeResult(True, "Хорошо, заявку отменил. Если понадобится помощь — просто напишите 💛", "cancelled")
        if _status(text):
            if status == "confirm_parent":
                return IntakeResult(True, "Заявка пока не отправлена специалисту — сначала подтвердите сводку кнопкой «Передать специалисту».", "confirm_parent", True)
            return IntakeResult(True, "Я ещё собираю данные для заявки. Продолжим с того места, где остановились.", "collecting")
        if status == "confirm_parent":
            if _yes(text):
                return await self.parent_decision(user, confirm=True)
            return IntakeResult(True, self._summary(scenario, fields), "confirm_parent", True)
        if status == "submitting":
            return IntakeResult(True, "Заявка уже отправляется. Подождите, пожалуйста, несколько секунд.", "submitting")

        step = next((s for s in scenario.steps if s.field == step_name), None)
        if step is None:
            step = self._next_step(scenario, fields)
        if step is None:
            self._save(user, scenario, "confirm_parent", payload, status="confirm_parent")
            return IntakeResult(True, self._summary(scenario, fields), "confirm_parent", True)

        ok, value, error = self._parse(step, text)
        if not ok:
            return IntakeResult(True, (error or "Не получилось распознать ответ.") + "\n\n" + step.prompt, "collecting")
        assert value is not None
        fields[step.field] = value

        if scenario.key == "transfer" and fields.get("current_shift") and fields.get("desired_shift") and fields["current_shift"] == fields["desired_shift"]:
            fields.pop("desired_shift", None)
            self._save(user, scenario, "desired_shift", payload)
            return IntakeResult(True, "Текущая и желаемая смена совпадают. Напишите, пожалуйста, на какую другую смену хотите перенести ребёнка.", "collecting")

        next_step = self._next_step(scenario, fields)
        if next_step is not None:
            self._save(user, scenario, next_step.field, payload)
            return IntakeResult(True, next_step.prompt, "collecting")

        self._save(user, scenario, "confirm_parent", payload, status="confirm_parent")
        return IntakeResult(True, self._summary(scenario, fields), "confirm_parent", True)

    async def parent_decision(self, user: IntakeUser, *, confirm: bool) -> IntakeResult:
        row = self.db.get_intake_session(user_id=user.user_id)
        if not row or str(row.get("status") or "") != "confirm_parent":
            return IntakeResult(True, "Эта кнопка уже неактуальна. Если нужна новая заявка — напишите вопрос ещё раз.", "stale")
        key = str(row.get("intake_type") or "")
        if key not in SCENARIOS:
            self.db.clear_intake_session(user_id=user.user_id)
            return IntakeResult(True, "Эта заявка уже неактуальна.", "stale")
        if not confirm:
            self.db.clear_intake_session(user_id=user.user_id)
            return IntakeResult(True, "Хорошо, заявку отменил. Ничего специалисту не отправлял.", "cancelled")

        claimed = self.db.claim_intake_submission(user_id=user.user_id)
        if not claimed:
            return IntakeResult(True, "Эта заявка уже отправляется или была обработана.", "stale")
        payload = dict(claimed.get("payload") or {})
        scenario = self._scenario(key)
        return IntakeResult(True, "Передаю заявку специалисту…", "submit", submission=self._submission(scenario, payload))

    def submission_finished(self, user_id: int, *, success: bool) -> None:
        if success:
            self.db.clear_intake_session(user_id=user_id)
        else:
            self.db.release_intake_submission(user_id=user_id)


# Backward-compatible public name used by the VK adapter/release docs.
SmartHandoffEngine = SmartIntakeEngine
