from __future__ import annotations

import json
import re
from dataclasses import dataclass


from .config import Settings
from .knowledge import Evidence, KnowledgeBase

SENSITIVE_PATTERNS = [
    r"\bдиагноз\w*\b", r"\bастм\w*\b", r"\bаллерг\w*\b", r"\bэпилеп\w*\b",
    r"\bинсулин\w*\b", r"\bинвалид\w*\b", r"\bхроничес\w*\b", r"\bназначен\w*\b",
    r"\bдозиров\w*\b", r"\bпаспорт\w*\b", r"\bснилс\w*\b", r"\bномер\s+полис\w*\b",
    r"\bсвидетельств\w*\s+о\s+рождени\w*\b.*\d", r"\bмедицинск\w*\s+данн\w*\b",
    r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}",
    r"(?:\+?7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}",
]

UNCONFIRMED_POLICY_PATTERNS = [
    # Payment / cancellation / legal-financial conditions not explicitly described in the knowledge base.
    r"\b(?:возврат|вернут|вернуть|отмен\w*\s+брон|отказ\w*\s+от\s+путев|расторг)\w*",
    r"\b(?:рассроч|кредит|оплат\w*\s+карт|наличн\w*|налогов\w*\s+вычет|компенсац)\w*",
    # Special diets and exact menu/portion details require a human unless explicitly published later.
    r"\b(?:диетическ|вегетариан|безглютен|безлактоз|аллергическ)\w*\s+(?:питан|меню)",
    r"\b(?:что\s+на\s+(?:завтрак|обед|ужин)|точн\w*\s+меню|размер\w*\s+порц|сколько\s+грамм)\b",
    r"\bдокуп\w*\s+питан",
    # The site does not publish phone-custody/call-time rules.
    r"\b(?:забира\w*|сдава\w*|храня\w*)\s+(?:телефон|смартфон)",
    r"\b(?:когда|во\s+сколько)\s+можно\s+(?:звонить|позвонить)\b",
    # Visiting time / taking a child away temporarily is not specified by the generic visits FAQ.
    r"\b(?:в\s+какое|в\s+какие\s+дни|какие\s+дни|во\s+сколько|время|часы)\w*.*\b(?:навещ|посещ)",
    r"\bзабра\w*\s+ребен\w*\s+(?:на\s+выходн|на\s+день|временно)",
    # Group/class arrivals are a separate service scenario, not ordinary parent visits.
    r"\b(?:классом|группой|командой)\b.*\b(?:приех|заезд|лагер)|\b(?:приех|заезд|лагер)\w*.*\b(?:классом|группой|командой)\b",
    # Seasons not currently published as shifts must never map to generic summer-shift FAQ.
    r"\b(?:осенн|весенн)\w*\s+смен",
    # Discounts outside the explicitly published categories must not be inferred from the generic discounts FAQ.
    r"\bскидк\w*.*\b(?:многодет|пенсионер|инвалид|сво|ветеран|малоимущ)\w*",
    # Seasonal/group offers and exact booking conditions from old chats change and need a manager/current temporary fact.
    r"\b(?:новогодн|новый\s+год)\w*.*\b(?:программ|предложен|выезд|мероприят)\w*",
    r"\b(?:услови\w*\s+бронирован|предоплат|аванс)\w*.*\b(?:бесед|корпус|домик|ночев|групп|класс)\w*",
]


def looks_unconfirmed_policy(text: str) -> bool:
    n = text.lower().replace("ё", "е")
    # Detailed group/class overnight packages depend on current group size, date and approved offer.
    if (
        any(x in n for x in ["класс", "групп", "команд"])
        and any(x in n for x in ["ночев", "корпус"])
        and any(x in n for x in ["что входит", "стоим", "сколько", "услови бронир", "предоплат", "аванс"])
    ):
        return True
    # Seasonal announcements are only auto-answered while a dated approved live-dialog FAQ is active.
    # If that FAQ expires, this catches both word orders and sends the question to a person.
    if any(x in n for x in ["новый год", "новогодн"]) and any(x in n for x in ["предлож", "программ", "выезд", "мероприят"]):
        return True
    # The current site explicitly offers group/class formats among extra services. A question such as
    # "можно приехать классом на квест?" is therefore allowed to reach the verified extra-services FAQ.
    group_service_context = (
        any(x in n for x in ["классом", "группой", "командой"])
        and any(x in n for x in ["квест", "мероприят", "бесед", "аренд", "ночев", "услуг"])
    )
    for pattern in UNCONFIRMED_POLICY_PATTERNS:
        if group_service_context and "классом" in pattern:
            continue
        if re.search(pattern, n, re.IGNORECASE):
            return True
    return False


def looks_specific_medicine_permission(text: str) -> bool:
    n = text.lower().replace("ё", "е")
    return bool(re.search(r"\b(?:какие|какое|какой)\s+лекарств\w*\s+(?:можно|разреш)", n))


DYNAMIC_AVAILABILITY_PATTERNS = [
    r"\bесть\s+мест", r"\bмест\w*.*\bесть\b", r"\bостал\w*\s+мест", r"\bсвободн\w*\s+мест",
    r"\bесть\s+путев\w*", r"\bпутев\w*.*\b(?:есть|остал|свобод|налич)\w*", r"\b(?:есть|остал|свобод|налич)\w*.*\bпутев\w*",
    r"\bсколько\s+путев\w*.*\bостал\w*",
    r"\bдоступн\w*\s+(?:смен|путев|мест)", r"\bближайш\w*\s+смен",
    r"\b(?:сейчас|сегодня)\b.*\b(?:путев|мест|заброни|купить)",
    r"\b(?:забронировать|купить)\b.*\b(?:сейчас|сегодня|мест)",
    r"\bвсе\s+(?:места\s+)?(?:занят|забронир)", r"\bмест\s+нет\b", r"\bместа\s+законч",
    # Service slots are just as volatile as camp capacity.
    r"\bсвободн\w*\s+(?:ли\s+)?(?:дат|врем|бесед|корпус|домик)",
    r"\bесть\s+ли\s+свободн\w*\s+(?:дат|врем|бесед|корпус|домик)",
    r"\b(?:бесед|корпус|домик)\w*.*\bсвободн\w*",
    r"\b(?:дат|врем)\w*.*\b(?:занят|свободн|доступн)\w*",
    r"\b(?:занят|забронир)\w*\s+(?:дат|бесед|корпус|домик)",
    r"\bесть\s+свободн\w*\s+(?:бесед|корпус|домик)",
]


def looks_dynamic_availability(text: str) -> bool:
    n = text.lower().replace("ё", "е")
    if any(re.search(p, n, re.IGNORECASE) for p in DYNAMIC_AVAILABILITY_PATTERNS):
        return True
    if "сейчас" in n and "доступ" in n and any(k in n for k in ["смен", "путев", "мест"]):
        return True
    return False




def looks_camp_address_request(text: str) -> bool:
    n = text.lower().replace("ё", "е")
    # The website publishes the sales-office address, but not a plain-text postal address of the camp itself.
    # Never silently substitute the office address when a parent asks specifically for the camp address.
    if "офис" in n:
        return False
    return bool(re.search(r"\bадрес\w*\b.*\b(?:лагер|звездочк)\w*\b|\b(?:лагер|звездочк)\w*\b.*\bадрес\w*\b", n))

def looks_current_health_situation(text: str) -> bool:
    n = text.lower().replace("ё", "е")
    health_terms = [
        "температур", "каш", "насморк", "заболел", "заболела", "болеет", "ветрянк",
        "грипп", "орви", "ангин", "рвот", "понос", "сып", "перелом", "травм",
    ]
    if not any(term in n for term in health_terms):
        return False
    individual_markers = [
        "у сына", "у дочери", "у ребенка", "ребенок сейчас", "мы забол",
        "можно ехать", "можно в лагерь", "примут", "допустят", "можно ли ехать",
    ]
    return any(marker in n for marker in individual_markers)


def looks_volatile_fact(text: str) -> bool:
    n = text.lower().replace("ё", "е")
    patterns = [
        r"\b(?:сколько\s+стоит|стоимост|цена|скидк)\w*",
        r"\b(?:когда|даты?|начина|заканчи)\w*.*\bсмен\w*",
        r"\bсмен\w*.*\b(?:когда|даты?|начина|заканчи)\w*",
        r"\b(?:заезд|выезд)\b",
        r"\b(?:телефон|номер\s+телефон|почт|email|e-mail|адрес|график\s+работ|работает\s+офис)\w*",
    ]
    return any(re.search(p, n, re.IGNORECASE) for p in patterns)


def unsafe_for_manual_faq(text: str) -> bool:
    # Legacy helper: older imports are permanent by definition.
    return not manual_knowledge_allowed(text, kind="permanent")[0]


def manual_knowledge_allowed(text: str, *, kind: str = "permanent") -> tuple[bool, str]:
    """Validate what a manager may add to the live knowledge base.

    Current availability and individual/sensitive situations are never cacheable.
    Volatile facts (prices, dates, contacts, office hours) are allowed only as temporary knowledge.
    Explicit manager approval may intentionally add a policy that was previously "unconfirmed".
    """
    if looks_dynamic_availability(text):
        return False, "dynamic_availability"
    if looks_current_health_situation(text) or looks_sensitive_or_individual(text):
        return False, "sensitive_or_individual"
    if looks_specific_medicine_permission(text):
        return False, "medicine_permission_requires_human"
    if kind != "temporary" and looks_volatile_fact(text):
        return False, "volatile_requires_temporary"
    return True, "ok"


def looks_sensitive_or_individual(text: str) -> bool:
    n = text.lower().replace("ё", "е")
    if any(re.search(p, n, re.IGNORECASE) for p in SENSITIVE_PATTERNS):
        return True
    # Long digit sequences often mean a document/policy/phone number. Do not send those to an LLM.
    if re.search(r"\b\d{6,}\b", n):
        return True
    return False


@dataclass
class BotAnswer:
    supported: bool
    text: str | None
    source: str | None = None
    confidence: float = 0.0
    reason: str = ""
    faq_id: str | None = None


class StrictResponder:
    def __init__(self, settings: Settings, kb: KnowledgeBase):
        self.settings = settings
        self.kb = kb
        self.client = None
        if settings.openai_api_key:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "OPENAI_API_KEY задан, но пакет openai не установлен. Выполните pip install -r requirements.txt."
                ) from exc
            self.client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def answer_faq_id(self, faq_id: str) -> BotAnswer:
        """Answer a trusted UI intent by stable FAQ id.

        Explicit buttons are not free-form user claims, so they must not be rejected by
        lexical guards such as the word "сейчас". The FAQ is still returned only when
        KnowledgeBase confirms that its backing snapshot is fresh and contains the
        verification markers.
        """
        faq = self.kb.get_faq_by_id(faq_id)
        if not faq:
            return BotAnswer(False, None, confidence=0.0, reason="intent_faq_unavailable", faq_id=faq_id)
        return BotAnswer(True, faq.answer, faq.source, 1.0, reason="approved_intent", faq_id=faq.faq_id)

    async def answer(self, question: str) -> BotAnswer:
        # Rule 0: facts that change minute-by-minute and individual/sensitive situations never come from cache.
        if looks_dynamic_availability(question):
            return BotAnswer(False, None, confidence=0.0, reason="dynamic_availability")
        if looks_specific_medicine_permission(question):
            return BotAnswer(False, None, confidence=0.0, reason="medicine_permission_requires_human")
        if looks_current_health_situation(question) or looks_sensitive_or_individual(question):
            return BotAnswer(False, None, confidence=0.0, reason="sensitive_or_individual")

        # Rule 1: an explicitly approved manager knowledge item may answer a policy question that the public site
        # does not document. This is what makes the in-chat knowledge editor useful without weakening hard safety.
        faq = self.kb.find_faq(question)
        if faq and faq.score >= self.settings.faq_min_score and (
            str(faq.source).startswith("approved_") or str(faq.source).startswith("official_live_dialog")
        ):
            return BotAnswer(True, faq.answer, faq.source, faq.score, reason="approved_manager_faq", faq_id=faq.faq_id)

        # Rule 2: topics whose exact conditions are not published/approved must not be answered by fuzzy similarity.
        if looks_unconfirmed_policy(question):
            return BotAnswer(False, None, confidence=0.0, reason="unconfirmed_policy")

        # Rule 3: do not confuse the published sales-office address with the physical camp address.
        if looks_camp_address_request(question):
            return BotAnswer(False, None, confidence=0.0, reason="camp_address_not_confirmed")

        # Rule 4: curated/approved public FAQ is the safest automatic source for the remaining topics.
        if faq and faq.score >= self.settings.faq_min_score:
            return BotAnswer(True, faq.answer, faq.source, faq.score, reason="approved_faq", faq_id=faq.faq_id)

        evidence = self.kb.search(question, limit=5)
        if not evidence or evidence[0].score < self.settings.retrieval_min_score:
            return BotAnswer(
                False,
                None,
                confidence=evidence[0].score if evidence else 0.0,
                reason="insufficient_evidence",
            )

        if not self.client:
            # No AI key: we intentionally do NOT paraphrase weaker retrieval. Human escalation is safer.
            return BotAnswer(False, None, confidence=evidence[0].score, reason="ai_disabled")

        return await self._llm_verify_and_answer(question, evidence)

    async def _llm_verify_and_answer(self, question: str, evidence: list[Evidence]) -> BotAnswer:
        context = "\n\n".join(
            f"SOURCE {i + 1} [{e.source_kind}]: {e.source}\n{e.text}" for i, e in enumerate(evidence)
        )
        prompt = f"""
Ты — первая линия поддержки детского лагеря «Звёздочка».
Главное правило: НЕ ДОБАВЛЯЙ НИ ОДНОГО ФАКТА, которого нет в источниках ниже.

Разрешено:
- понять разговорную формулировку родителя;
- кратко и дружелюбно переформулировать ТОЧНО подтверждённые сведения;
- отвечать как сотрудник лагеря: естественно, вежливо и по существу.

Стиль ответа:
- сначала прямо ответь на то, что спросил родитель;
- не пиши «на сайте указано», «согласно сайту», «в источнике сказано» и подобные служебные фразы;
- не здоровайся заново в каждом сообщении;
- не добавляй ссылку на источник в текст ответа — интерфейс добавит её сам.

Запрещено:
- использовать внешние знания, догадки, здравый смысл или вероятные ответы;
- объединять сведения так, чтобы появлялся новый неподтверждённый факт;
- отвечать на индивидуальные медицинские, юридические или персональные ситуации;
- считать отсутствие информации подтверждением отрицательного ответа.

Приоритет доверия: approved_faq > website > vk_profile > vk_wall_recent.
Если сведения из более слабого источника противоречат более сильному, не выбирай слабый источник.
Если точного ответа нет, сведения неполные, неоднозначные или противоречат друг другу — supported=false.

ВОПРОС РОДИТЕЛЯ:
{question}

ИСТОЧНИКИ:
{context}

Верни ТОЛЬКО JSON без markdown:
{{"supported": true|false, "answer": "краткий ответ либо пустая строка", "source_number": 1|2|3|4|5|null}}
""".strip()

        try:
            response = await self.client.responses.create(
                model=self.settings.openai_model,
                input=prompt,
                text={"verbosity": "low"},
                reasoning={"effort": "low"},
                store=False,
            )
            raw = response.output_text.strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                raw = raw.removeprefix("json").strip()
            data = json.loads(raw)
            if data.get("supported") is not True:
                return BotAnswer(False, None, confidence=evidence[0].score, reason="ai_rejected")
            answer = str(data.get("answer", "")).strip()
            idx = data.get("source_number")
            if not answer or not isinstance(idx, int) or not 1 <= idx <= len(evidence):
                return BotAnswer(False, None, confidence=evidence[0].score, reason="ai_invalid_output")
            source = evidence[idx - 1].source
            return BotAnswer(True, answer, source, evidence[0].score, reason="grounded_ai")
        except Exception as exc:
            print(f"[llm] verification failed: {type(exc).__name__}: {exc}")
            return BotAnswer(False, None, confidence=evidence[0].score, reason="ai_error")
