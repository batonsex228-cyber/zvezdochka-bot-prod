from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import quote

from .responder import BotAnswer

OFFICE_ADDRESS = "г. Глазов, ул. Ленина, д. 11Г"
OFFICE_MAP_URL = "https://yandex.ru/maps/?text=" + quote("Глазов, улица Ленина, 11г")
PHONE_DISPLAY = "+7 (982) 792-81-08"
PHONE_URL = "tel:+79827928108"
EMAIL = "dzl_razvitie@mail.ru"


@dataclass(frozen=True)
class PresentedAnswer:
    text: str
    preview_url: str | None = None


def _n(text: str) -> str:
    return text.lower().replace("ё", "е")


def _link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def _more(source: str | None, label: str = "Узнать подробнее") -> str:
    if not source or not source.startswith("http"):
        return ""
    return f"\n\n🔎 {_link(source, label)}"


def _bullets(items: list[str]) -> str:
    return "\n".join(f"• {item}" for item in items)


def _contact_intent(question: str) -> str:
    q = _n(question)
    if "адрес" in q or ("где" in q and "офис" in q):
        return "address"
    if any(x in q for x in ["телефон", "номер", "позвон", "связаться по телефону"]):
        return "phone"
    if any(x in q for x in ["почт", "email", "e-mail", "емейл"]):
        return "email"
    if any(x in q for x in ["график", "до сколь", "во сколько", "когда работает", "время работы", "рабочие дни"]):
        return "hours"
    return "general"


def _wants_price(question: str) -> bool:
    q = _n(question)
    return any(x in q for x in ["стоим", "цен", "сколько стоит", "руб", "путев"])


def _wants_dates(question: str) -> bool:
    q = _n(question)
    return any(x in q for x in ["когда", "дат", "начина", "заканчи", "с какого", "по какое"])


def _contacts(question: str, source: str | None) -> PresentedAnswer:
    intent = _contact_intent(question)
    address = f"<b>{OFFICE_ADDRESS}</b>\n<i>цокольный этаж</i>"
    phone = _link(PHONE_URL, PHONE_DISPLAY)
    email_link = _link(f"mailto:{EMAIL}", EMAIL)
    map_link = f"<b>{_link(OFFICE_MAP_URL, 'Открыть офис в Яндекс Картах')}</b>"
    hours = "Пн–Пт — <b>09:00–18:00</b>\nОбед — 12:00–13:00\nСб–Вс — выходные"

    if intent == "address":
        text = (
            "📍 Наш <b>главный офис продаж</b> находится по адресу:\n\n"
            f"{address}\n\n"
            f"🗺 {map_link}\n\n"
            f"🕘 <b>Когда можно прийти:</b>\n{hours}\n\n"
            "Если понадобится связаться с нами:\n"
            f"📞 <b>Телефон:</b> {phone}\n"
            f"✉️ <b>Почта:</b> {email_link}"
            + _more(source, "Все контакты")
        )
        return PresentedAnswer(text=text, preview_url=OFFICE_MAP_URL)

    if intent == "phone":
        text = (
            "📞 Позвонить нам можно по номеру:\n\n"
            f"<b>{phone}</b>\n\n"
            f"🕘 <b>График офиса:</b>\n{hours}\n\n"
            f"✉️ Если удобнее написать: {email_link}"
            + _more(source, "Все контакты")
        )
        return PresentedAnswer(text=text)

    if intent == "email":
        text = (
            "✉️ Наша электронная почта:\n\n"
            f"<b>{email_link}</b>\n\n"
            f"📞 Телефон: {phone}\n"
            f"📍 Офис: <b>{OFFICE_ADDRESS}</b>"
            + _more(source, "Все контакты")
        )
        return PresentedAnswer(text=text)

    if intent == "hours":
        text = (
            "🕘 <b>Главный офис продаж</b> работает так:\n\n"
            f"{hours}\n\n"
            f"📍 {OFFICE_ADDRESS}, цокольный этаж\n"
            f"🗺 {map_link}"
            + _more(source, "Все контакты")
        )
        return PresentedAnswer(text=text, preview_url=OFFICE_MAP_URL)

    text = (
        "📞 Вот наши основные контакты:\n\n"
        f"<b>Телефон:</b> {phone}\n\n"
        f"<b>Почта:</b> {email_link}\n\n"
        f"<b>Главный офис продаж:</b>\n{address}\n\n"
        f"🕘 <b>График:</b>\n{hours}\n\n"
        f"🗺 {map_link}"
        + _more(source, "Все контакты")
    )
    return PresentedAnswer(text=text, preview_url=OFFICE_MAP_URL)


def _shift(question: str, source: str | None, title: str, dates: str, price: str) -> PresentedAnswer:
    price_first = _wants_price(question) and not _wants_dates(question)
    if price_first:
        body = (
            f"💳 <b>{title}:</b> стоимость путёвки — <b>{price}</b>.\n\n"
            f"📅 Даты смены: <b>{dates}</b>."
        )
    else:
        body = (
            f"📅 <b>{title}</b> проходит <b>{dates}</b>.\n\n"
            f"💳 Стоимость путёвки — <b>{price}</b>."
        )
    return PresentedAnswer(body + _more(source, "Подробнее о смене"))


def present_answer(question: str, answer: BotAnswer, show_source_links: bool = True) -> PresentedAnswer:
    source = answer.source if show_source_links else None
    fid = answer.faq_id

    if fid == "contacts":
        return _contacts(question, source)

    if fid == "discounts":
        text = (
            "💳 Да, у нас есть скидки 😊\n\n"
            "<b>Скидка — 5%</b> в следующих случаях:\n\n"
            "• при покупке <b>двух смен</b> для одного ребёнка;\n"
            "• при покупке путёвок для <b>двух и более детей</b>;\n"
            "• для сотрудников <b>МВД, ФСИН, ФСБ, МЧС и Вооружённых сил РФ</b>."
            + _more(source, "Подробнее о путёвках и скидках")
        )
        return PresentedAnswer(text)

    if fid == "food-frequency":
        return PresentedAnswer(
            "🍲 В лагере предусмотрено <b>пятиразовое питание</b> — полноценное меню каждый день."
            + _more(source, "Подробнее о лагере")
        )

    if fid == "summer-shifts-2026":
        text = (
            "📅 Сейчас у «Звёздочки» опубликованы <b>зимняя и летние смены</b>.\n\n"
            "❄️ <b>Зимняя смена «Тайны Северного сияния»</b>\n"
            "3–8 января 2027 года\n"
            "Стоимость — <b>13 000 ₽</b>.\n\n"
            "☀️ <b>Летние смены 2026 года:</b>\n"
            "1-я — 18 июня — 2 июля\n"
            "2-я — 5 — 19 июля\n"
            "3-я — 22 июля — 5 августа\n"
            "4-я — 8 — 22 августа\n\n"
            "Стоимость каждой летней путёвки — <b>34 000 ₽</b>.\n\n"
            "<i>Ниже можно сразу открыть нужную форму записи.</i>"
            + _more(source, "Посмотреть все смены")
        )
        return PresentedAnswer(text)

    if fid == "all-prices":
        text = (
            "💳 Стоимость опубликованных путёвок такая:\n\n"
            "❄️ <b>Зимняя смена 3–8 января 2027:</b> <b>13 000 ₽</b>\n\n"
            "☀️ <b>Летние смены 2026:</b> по <b>34 000 ₽</b> каждая.\n\n"
            "<i>Ниже можно сразу открыть форму нужной смены.</i>"
            + _more(source, "Подробнее о сменах и стоимости")
        )
        return PresentedAnswer(text)

    shifts = {
        "shift-1-2026": ("1-я летняя смена", "с 18 июня по 2 июля 2026 года", "34 000 ₽"),
        "shift-2-2026": ("2-я летняя смена", "с 5 по 19 июля 2026 года", "34 000 ₽"),
        "shift-3-2026": ("3-я летняя смена", "с 22 июля по 5 августа 2026 года", "34 000 ₽"),
        "shift-4-2026": ("4-я летняя смена", "с 8 по 22 августа 2026 года", "34 000 ₽"),
        "winter-2027": ("зимняя смена «Тайны Северного сияния»", "с 3 по 8 января 2027 года", "13 000 ₽"),
    }
    if fid in shifts:
        return _shift(question, source, *shifts[fid])

    if fid == "age":
        return PresentedAnswer(
            "⭐ Смены рассчитаны на детей и подростков <b>от 7 до 17 лет</b>."
            + _more(source, "Подробнее о лагере")
        )

    if fid == "arrival":
        text = (
            "🚗 Заезд и выезд проходят <b>с 08:00 до 11:00</b>.\n\n"
            "Ребёнка необходимо привезти в лагерь <b>самостоятельно</b>."
            + _more(source, "Подробнее о заезде")
        )
        return PresentedAnswer(text)

    if fid == "transfer":
        return PresentedAnswer(
            "🚗 <b>Трансфер в стоимость путёвки не входит.</b>\n\n"
            "Ребёнка необходимо привезти в лагерь самостоятельно."
            + _more(source, "Подробнее о заезде")
        )

    if fid == "contract-documents-live":
        text = (
            "📄 Для оформления договора возьмите:\n\n"
            "• <b>паспорт родителя</b>;\n"
            "• свидетельство о рождении ребёнка (или паспорт);\n"
            "• СНИЛС ребёнка;\n"
            "• полис ОМС ребёнка.\n\n"
            "<i>Это список именно для оформления договора. Для самого заезда медицинские документы проверяются отдельно.</i>"
        )
        return PresentedAnswer(text)

    if fid == "phone-custody-live":
        return PresentedAnswer(
            "📱 Да. <b>Телефоны хранятся у вожатых</b>.\n\n"
            "Детям их выдают <b>с 16:30 до 17:30</b>."
        )

    if fid == "ndfl-documents-live":
        return PresentedAnswer(
            "🧾 Да, документы для <b>возврата НДФЛ за путёвку</b> выдаются.\n\n"
            "Для подготовки товарной накладной сотруднику понадобятся:\n"
            "• ФИО ребёнка;\n• ФИО родителя;\n• номер смены.\n\n"
            "🔐 <i>Персональные данные лучше передать сотруднику напрямую, а не отправлять их боту.</i>"
        )

    if fid == "documents":
        items = [
            "анкета ребёнка;",
            "добровольное согласие на медицинское вмешательство;",
            "договор с родителями;",
            "памятка для родителей;",
            "согласие на пребывание ребёнка;",
            "страховка;",
            "справка <b>079/у</b> с записью о прививках, отсутствии контакта с инфекциями и противопоказаний;",
            "анализы на яйца гельминтов и энтеробиоз;",
            "копия полиса ОМС;",
            "согласие на обработку персональных данных;",
            "копия свидетельства о рождении или паспорта — с 14 лет.",
        ]
        text = (
            "📄 Для поездки понадобится следующий комплект документов:\n\n"
            + _bullets(items)
            + "\n\n<i>Лучше подготовить документы заранее, чтобы перед заездом ничего не искать в последний момент 😊</i>"
            + _more(source, "Документы для родителей")
        )
        return PresentedAnswer(text)

    if fid == "prohibited-food":
        items = [
            "газированные напитки;",
            "пирожные и торты;",
            "сухарики, чипсы и семечки;",
            "молочные продукты;",
            "мясо, птицу, рыбу, копчёности и колбасы;",
            "грибы;",
            "ягоды и цитрусовые;",
            "супы, пюре и лапшу быстрого приготовления;",
            "фастфуд.",
        ]
        text = "🚫 Из еды в лагерь <b>нельзя брать</b>:\n\n" + _bullets(items) + _more(source, "Полный список для родителей")
        return PresentedAnswer(text)

    if fid == "prohibited-items":
        items = [
            "спички и зажигалки;",
            "пиротехнику;",
            "сигареты и электронные сигареты;",
            "спиртные напитки;",
            "режущие и колющие предметы — кроме маникюрных ножниц.",
        ]
        text = "🚫 <b>Категорически запрещено</b> брать с собой:\n\n" + _bullets(items) + _more(source, "Правила для родителей")
        return PresentedAnswer(text)

    if fid == "not-recommended":
        items = [
            "дорогие смартфоны, ноутбуки, игровые консоли и планшеты;",
            "большие суммы денег;",
            "ювелирные украшения;",
            "аэрозоли и фумигатор.",
        ]
        text = (
            "⚠️ Лучше не брать с собой:\n\n"
            + _bullets(items)
            + "\n\n<blockquote><b>Важно:</b> лекарства ребёнку также не рекомендуют хранить самостоятельно — их нужно передать сотруднику лагеря.</blockquote>"
            + _more(source, "Что взять с собой")
        )
        return PresentedAnswer(text)

    if fid == "medicines-general":
        text = (
            "💊 Если ребёнку нужны постоянные лекарства, <b>заранее сообщите об этом сотруднику лагеря</b>.\n\n"
            "<blockquote>Лекарства нужно передать взрослому сотруднику лично вместе с понятной инструкцией по приёму. "
            "Не оставляйте их ребёнку для самостоятельного хранения.</blockquote>"
            + _more(source, "Информация для родителей")
        )
        return PresentedAnswer(text)

    if fid == "illness-general":
        text = (
            "🩺 Если ребёнок заболеет во время смены, его <b>осмотрит медицинский работник</b>.\n\n"
            "Родителям сообщат о состоянии ребёнка и дальнейших действиях."
            + _more(source, "Информация для родителей")
        )
        return PresentedAnswer(text)

    if fid == "visits":
        text = (
            "👨‍👩‍👧 Да, посещения возможны 😊\n\n"
            "Они проходят <b>по правилам конкретной смены</b> и после предварительного согласования с администрацией лагеря."
            + _more(source, "Информация для родителей")
        )
        return PresentedAnswer(text)

    if fid == "bring-all":
        text = (
            "🎒 Вот основное, что стоит собрать ребёнку:\n\n"
            "👕 <b>Одежда:</b> по погоде, тёплые вещи, одежда для спорта, пижама, головной убор.\n\n"
            "👟 <b>Обувь:</b> спортивная, сменная, сандалии/босоножки, шлёпки и домашние тапочки.\n\n"
            "🧴 <b>Гигиена:</b> зубная щётка и паста, шампунь, гель для тела, салфетки, солнцезащитный крем, средство от комаров — <b>не аэрозоль</b>.\n\n"
            "<i>Можете написать мне «одежда», «обувь» или «гигиена» — дам подробный список по нужному разделу.</i>"
            + _more(source, "Полный список вещей")
        )
        return PresentedAnswer(text)

    if fid == "clothes":
        items = [
            "шорты или юбки;", "футболки и майки;", "непромокаемую куртку или дождевик;",
            "тёплый свитер или толстовку;", "одежду с длинным рукавом;", "длинные штаны или джинсы;",
            "носки;", "спортивный костюм;", "нижнее бельё;", "пижаму;", "головной убор.",
        ]
        return PresentedAnswer("👕 Из одежды рекомендуем взять:\n\n" + _bullets(items) + _more(source, "Полный список вещей"))

    if fid == "shoes":
        items = ["спортивную обувь;", "сандалии или босоножки;", "шлёпки или сланцы;", "домашние тапочки."]
        return PresentedAnswer("👟 Из обуви рекомендуем взять:\n\n" + _bullets(items) + _more(source, "Полный список вещей"))

    if fid == "hygiene":
        items = [
            "расчёску;", "солнцезащитный крем;", "сухие и влажные салфетки;", "зубную щётку и пасту;",
            "шампунь;", "гель для тела;", "крем, спрей или браслет от комаров — <b>не аэрозоль</b>.",
        ]
        return PresentedAnswer("🧴 Из средств личной гигиены рекомендуем взять:\n\n" + _bullets(items) + _more(source, "Полный список вещей"))

    if fid == "territory":
        text = (
            "🌲 На территории лагеря есть всё основное для проживания и активного отдыха:\n\n"
            "🏠 <b>Инфраструктура:</b> благоустроенные корпуса, медицинский кабинет, столовая, душевые и прачечная.\n\n"
            "⚽️ <b>Для занятий и отдыха:</b> стадион, теннисный корт, летняя эстрада, игровые и спортивные площадки, кружковые пространства.\n\n"
            "🛡 <b>Безопасность:</b> круглосуточная охрана и видеонаблюдение."
            + _more(source, "Подробнее о лагере")
        )
        return PresentedAnswer(text)

    if fid == "extra-services":
        q = _n(question)
        if "бесед" in q:
            text = (
                "🌿 Да 😊 <b>Аренда беседки</b> доступна.\n\n"
                "Стоимость — <b>2 700 ₽ за беседку</b> на 3 часа.\n\n"
                "В описании услуги указаны мангал, уголь, розжиг и решётка.\n\n"
                "<i>Чтобы оставить заявку, нажмите кнопку под сообщением.</i>"
                + _more(source, "Подробнее об аренде беседок")
            )
            return PresentedAnswer(text)
        if "ночев" in q or "корпус" in q:
            text = (
                "🏠 Да, можно организовать <b>выездное мероприятие с ночёвкой</b> в отапливаемом корпусе.\n\n"
                "Стоимость — <b>800 ₽ с человека за сутки</b>. Питание можно согласовать дополнительно.\n\n"
                "<i>Заявку можно оставить по кнопке под сообщением.</i>"
                + _more(source, "Подробнее об услуге")
            )
            return PresentedAnswer(text)
        if "квест" in q:
            text = (
                "🧩 Да 😊 У лагеря есть несколько квестовых форматов:\n\n"
                "• QR-квест <b>«Разгадай загадки лагеря»</b> — <b>400 ₽ с человека</b>;\n"
                "• командный квест <b>«Тайна старого лагеря»</b> — стоимость по договорённости.\n\n"
                "<i>Чтобы оставить заявку, нажмите кнопку под сообщением.</i>"
                + _more(source, "Подробнее о квестах")
            )
            return PresentedAnswer(text)
        text = (
            "✨ У лагеря есть дополнительные услуги для детей и взрослых:\n\n"
            "• QR-квест <b>«Разгадай загадки лагеря»</b> — 400 ₽ с человека;\n"
            "• командный квест <b>«Тайна старого лагеря»</b> — стоимость по договорённости;\n"
            "• аренда беседки на 3 часа — <b>2 700 ₽</b>;\n"
            "• выездное мероприятие с ночёвкой в отапливаемом корпусе — <b>800 ₽ с человека за сутки</b>.\n\n"
            "<i>Если хотите забронировать услугу, нажмите кнопку под сообщением.</i>"
            + _more(source, "Подробнее о дополнительных услугах")
        )
        return PresentedAnswer(text)

    if fid == "new-year-class-offer-2026-live":
        return PresentedAnswer(
            "🎄 Да, <b>новогодние программы планируются</b> 😊\n\n"
            "Информацию о программах выложат в группе <b>после 22 сентября</b>."
        )

    if fid == "program":
        text = (
            "🎯 В лагере дети не сидят без дела 😊 В программе есть:\n\n"
            "• спортивные мероприятия;\n"
            "• робототехника;\n"
            "• творческие мастерские;\n"
            "• вечерние события;\n"
            "• командные активности."
            + _more(source, "Подробнее о программе")
        )
        return PresentedAnswer(text)

    # Manual FAQ or future FAQ without a dedicated template. Keep it human, safe and readable.
    clean = html.escape((answer.text or "").strip())
    clean = re.sub(r"\s*\n\s*", "\n", clean)
    text = clean + _more(source)
    return PresentedAnswer(text)
