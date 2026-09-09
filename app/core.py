from __future__ import annotations

from dataclasses import dataclass

from .presentation import PresentedAnswer, present_answer
from .responder import BotAnswer, StrictResponder

# Stable product intents. Explicit buttons and deterministic router output land here.
INTENT_FAQ: dict[str, str] = {
    "shifts_prices": "summer-shifts-2026",
    "shift_1": "shift-1-2026",
    "shift_2": "shift-2-2026",
    "shift_3": "shift-3-2026",
    "shift_4": "shift-4-2026",
    "winter": "winter-2027",
    "food": "food-frequency",
    "bring": "bring-all",
    "clothes": "clothes",
    "shoes": "shoes",
    "hygiene": "hygiene",
    "documents": "documents",
    "contract_documents": "contract-documents-live",
    "phone_policy": "phone-custody-live",
    "ndfl_documents": "ndfl-documents-live",
    "arrival": "arrival",
    "transfer": "transfer",
    "contacts": "contacts",
    "discounts": "discounts",
    "age": "age",
    "visits": "visits",
    "medicines": "medicines-general",
    "illness": "illness-general",
    "prohibited_food": "prohibited-food",
    "prohibited_items": "prohibited-items",
    "territory": "territory",
    "extra_services": "extra-services",
    "program": "program",
    "new_year_offer": "new-year-class-offer-2026-live",
}

MENU_LABEL_INTENTS: dict[str, str] = {
    "📅 Смены и цены": "shifts_prices",
    "🍲 Питание": "food",
    "🎒 Что взять": "bring",
    "📄 Документы": "documents",
    "🚗 Заезд": "arrival",
    "📞 Контакты": "contacts",
}

# Product-critical data shown by the primary button is intentionally pinned and does not depend
# on a crawler snapshot. Current availability is excluded because it changes quickly.
PINNED_UI_ANSWERS: dict[str, dict[str, str]] = {
    "shifts_prices": {
        "faq_id": "summer-shifts-2026",
        "source": "https://zvezdaglazov.ru/new/pages/trip.html",
        "text": (
            "📅 <b>Смены и цены</b>\n\n"
            "❄️ <b>Зимняя смена 2027</b>\n"
            "«Тайны Северного сияния»\n"
            "3–8 января 2027 года — <b>13 000 ₽</b>\n\n"
            "☀️ <b>Летние смены 2026</b>\n"
            "1 смена: 18 июня – 2 июля — <b>34 000 ₽</b>\n"
            "2 смена: 5–19 июля — <b>34 000 ₽</b>\n"
            "3 смена: 22 июля – 5 августа — <b>34 000 ₽</b>\n"
            "4 смена: 8–22 августа — <b>34 000 ₽</b>\n\n"
            "Количество свободных мест меняется, поэтому наличие путёвок лучше уточнять отдельно."
        ),
    }
}


@dataclass(frozen=True)
class CoreResult:
    requested_text: str
    intent: str | None
    answer: BotAnswer
    presented: PresentedAnswer | None

    @property
    def supported(self) -> bool:
        return self.answer.supported and bool(self.answer.text) and self.presented is not None


class SupportCore:
    """Platform-independent decision layer. VK is primary; MAX can reuse the same core later."""

    def __init__(self, responder: StrictResponder, *, show_source_links: bool = True):
        self.responder = responder
        self.show_source_links = show_source_links

    @staticmethod
    def detect_menu_intent(text: str) -> str | None:
        return MENU_LABEL_INTENTS.get(text.strip())

    async def process(self, text: str, *, intent: str | None = None) -> CoreResult:
        requested_text = text.strip()
        resolved_intent = intent or self.detect_menu_intent(requested_text)

        # Internal safety intent used by adapters when conversation context proves the parent
        # is asking for a live availability/booking check (for example a date-only follow-up).
        if resolved_intent == "dynamic_availability":
            answer = BotAnswer(False, None, confidence=0.0, reason="dynamic_availability")
            return CoreResult(requested_text, resolved_intent, answer, None)

        pinned = PINNED_UI_ANSWERS.get(resolved_intent or "")
        if pinned:
            answer = BotAnswer(
                supported=True,
                text=pinned["text"],
                source=pinned["source"],
                confidence=1.0,
                reason="approved_intent",
                faq_id=pinned["faq_id"],
            )
        elif resolved_intent:
            faq_id = INTENT_FAQ.get(resolved_intent)
            if not faq_id:
                answer = BotAnswer(False, None, confidence=0.0, reason="unknown_ui_intent")
                return CoreResult(requested_text, resolved_intent, answer, None)
            answer = await self.responder.answer_faq_id(faq_id)
        else:
            answer = await self.responder.answer(requested_text)

        if not answer.supported or not answer.text:
            return CoreResult(requested_text, resolved_intent, answer, None)

        presented = present_answer(requested_text, answer, show_source_links=self.show_source_links)
        return CoreResult(requested_text, resolved_intent, answer, presented)
