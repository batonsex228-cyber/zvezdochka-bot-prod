from __future__ import annotations

import json

from .links import CALLBACK_FORM, DOCUMENTS_DOWNLOAD, EXTRA_SERVICES_FORM, SUMMER_SHIFTS_FORM, WINTER_SHIFT_FORM


def _payload(**data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _text(label: str, intent: str, color: str = "secondary") -> dict:
    return {"color": color, "action": {"type": "text", "label": label, "payload": _payload(intent=intent)}}


def _callback(label: str, payload: dict, color: str = "secondary") -> dict:
    return {"color": color, "action": {"type": "callback", "label": label, "payload": _payload(**payload)}}


def _open_link(label: str, url: str) -> dict:
    return {"action": {"type": "open_link", "label": label, "link": url, "payload": "{}"}}


def _dump(buttons: list[list[dict]], *, inline: bool = True, one_time: bool = False) -> str:
    return json.dumps({"one_time": one_time, "inline": inline, "buttons": buttons}, ensure_ascii=False, separators=(",", ":"))


def main_keyboard() -> str:
    return _dump([
        [_text("📅 Смены и цены", "shifts_prices", "primary"), _text("🍲 Питание", "food")],
        [_text("🎒 Что взять", "bring"), _text("📄 Документы", "documents")],
        [_text("🚗 Заезд", "arrival"), _text("📞 Контакты", "contacts")],
        [_text("🔔 Важные уведомления", "notifications")],
        [_text("❓ Задать свой вопрос", "ask", "positive")],
    ], inline=False)


def answer_keyboard(faq_id: str | None, feedback_id: int | None = None) -> str | None:
    rows: list[list[dict]] = []
    if faq_id in {"summer-shifts-2026", "all-prices", "discounts"}:
        rows.append([_open_link("❄️ Зимняя путёвка", WINTER_SHIFT_FORM), _open_link("☀️ Летние путёвки", SUMMER_SHIFTS_FORM)])
    elif faq_id in {"shift-1-2026", "shift-2-2026", "shift-3-2026", "shift-4-2026"}:
        rows.append([_open_link("🎟 Купить путёвку", SUMMER_SHIFTS_FORM)])
    elif faq_id == "winter-2027":
        rows.append([_open_link("❄️ Купить зимнюю путёвку", WINTER_SHIFT_FORM)])
    elif faq_id == "extra-services":
        rows.append([_open_link("✨ Оставить заявку", EXTRA_SERVICES_FORM)])
    elif faq_id == "contacts":
        rows.append([_open_link("📞 Заказать обратный звонок", CALLBACK_FORM)])
    elif faq_id == "documents":
        rows.append([_open_link("📄 Скачать документы", DOCUMENTS_DOWNLOAD)])
    if feedback_id is not None:
        rows.append([
            _callback("👍 Помогло", {"action": "feedback", "value": "yes", "id": feedback_id}, "positive"),
            _callback("👎 Нужен специалист", {"action": "feedback", "value": "no", "id": feedback_id}, "negative"),
        ])
    return _dump(rows) if rows else None


def fallback_keyboard() -> str:
    return _dump([[_open_link("📞 Заказать обратный звонок", CALLBACK_FORM)]])


def clarification_keyboard(kind: str) -> str | None:
    if kind == "address":
        return _dump([[
            _text("🏢 Офис продаж", "contacts", "primary"),
            _callback("🏕 Адрес лагеря", {"action": "clarify", "intent": "camp_address"}, "secondary"),
        ]])
    return None


def notification_keyboard(subscribed: bool) -> str:
    if subscribed:
        return _dump([[_callback("🔕 Отключить уведомления", {"action": "notifications", "value": "off"}, "negative")]])
    return _dump([[_callback("✅ Получать уведомления", {"action": "notifications", "value": "on"}, "positive")]])


def manager_ticket_keyboard(ticket_id: int) -> str:
    return _dump([[_callback("✍ Ответить", {"action": "manager_reply", "ticket_id": ticket_id}, "primary")]])


def admin_keyboard() -> str:
    return _dump([
        [_callback("📊 Статистика", {"action": "admin", "cmd": "stats"}, "primary"), _callback("🆘 Открытые вопросы", {"action": "admin", "cmd": "open"})],
        [_callback("📢 Создать рассылку", {"action": "admin", "cmd": "newsletter"}, "positive")],
        [_callback("📚 База знаний", {"action": "admin", "cmd": "knowledge"}, "primary")],
        [_callback("👎 Проблемные ответы", {"action": "admin", "cmd": "bad"}), _callback("🔄 Диагностика", {"action": "admin", "cmd": "diag"})],
    ])


def newsletter_preview_keyboard(campaign_id: int) -> str:
    return _dump([
        [_callback("🚀 Отправить", {"action": "newsletter", "cmd": "send", "campaign_id": campaign_id}, "positive")],
        [_callback("✏ Изменить", {"action": "newsletter", "cmd": "edit", "campaign_id": campaign_id}),
         _callback("❌ Отмена", {"action": "newsletter", "cmd": "cancel", "campaign_id": campaign_id}, "negative")],
    ])


def shifts_carousel_template() -> str:
    elements = [
        {"title": "❄️ Зимняя смена", "description": "3–8 января 2027\n13 000 ₽", "buttons": [{"action": {"type": "open_link", "link": WINTER_SHIFT_FORM, "label": "Выбрать"}}]},
        {"title": "☀️ 1 смена", "description": "18 июня – 2 июля\n34 000 ₽", "buttons": [{"action": {"type": "open_link", "link": SUMMER_SHIFTS_FORM, "label": "Выбрать"}}]},
        {"title": "☀️ 2 смена", "description": "5–19 июля\n34 000 ₽", "buttons": [{"action": {"type": "open_link", "link": SUMMER_SHIFTS_FORM, "label": "Выбрать"}}]},
        {"title": "☀️ 3 смена", "description": "22 июля – 5 августа\n34 000 ₽", "buttons": [{"action": {"type": "open_link", "link": SUMMER_SHIFTS_FORM, "label": "Выбрать"}}]},
        {"title": "☀️ 4 смена", "description": "8–22 августа\n34 000 ₽", "buttons": [{"action": {"type": "open_link", "link": SUMMER_SHIFTS_FORM, "label": "Выбрать"}}]},
    ]
    return json.dumps({"type": "carousel", "elements": elements}, ensure_ascii=False, separators=(",", ":"))


def knowledge_menu_keyboard() -> str:
    return _dump([
        [_callback("➕ Постоянное знание", {"action": "knowledge", "cmd": "permanent"}, "positive")],
        [_callback("⏱ Временное знание", {"action": "knowledge", "cmd": "temporary"}, "primary")],
        [_callback("📋 Показать знания", {"action": "knowledge", "cmd": "list"})],
        [_callback("↩ В админ-меню", {"action": "knowledge", "cmd": "back"})],
    ])


def knowledge_ttl_keyboard() -> str:
    return _dump([
        [_callback("1 день", {"action": "knowledge", "cmd": "ttl", "days": 1}),
         _callback("7 дней", {"action": "knowledge", "cmd": "ttl", "days": 7})],
        [_callback("30 дней", {"action": "knowledge", "cmd": "ttl", "days": 30}),
         _callback("90 дней", {"action": "knowledge", "cmd": "ttl", "days": 90})],
        [_callback("❌ Отмена", {"action": "knowledge", "cmd": "cancel"}, "negative")],
    ])


def knowledge_preview_keyboard() -> str:
    return _dump([
        [_callback("✅ Сохранить", {"action": "knowledge", "cmd": "save"}, "positive")],
        [_callback("❌ Отмена", {"action": "knowledge", "cmd": "cancel"}, "negative")],
    ])
