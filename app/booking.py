from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .db import Database


class BookingAPIError(RuntimeError):
    pass


class BookingBackend(Protocol):
    async def call(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class AppsScriptBookingBackend:
    """Small JSON client for the Google Apps Script booking web app."""

    def __init__(self, settings: Settings):
        if not settings.booking_api_url or not settings.booking_api_secret:
            raise BookingAPIError("booking backend is not configured")
        self.url = settings.booking_api_url
        self.secret = settings.booking_api_secret
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.booking_timeout_seconds, connect=8.0),
            follow_redirects=True,
        )

    async def call(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self.client.post(
                self.url,
                json={"secret": self.secret, "action": action, "payload": payload or {}},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise BookingAPIError(f"booking API request failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(data, dict):
            raise BookingAPIError("booking API returned non-object JSON")
        if data.get("ok") is not True:
            raise BookingAPIError(str(data.get("error") or "booking API rejected request"))
        result = data.get("result")
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        await self.client.aclose()


@dataclass(frozen=True)
class BookingUser:
    user_id: int
    peer_id: int
    full_name: str
    username: str | None = None


@dataclass
class BookingResult:
    handled: bool
    text: str = ""
    state: str = ""
    booking_id: str | None = None
    manager_text: str | None = None
    error_reason: str | None = None

    @property
    def needs_parent_confirmation(self) -> bool:
        return self.state == "confirm_parent"


_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "май": 5, "мая": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower().replace("ё", "е"))


def _service_from_text(n: str) -> str | None:
    if any(x in n for x in ["бесед", "газеб"]):
        return "gazebo"
    if any(x in n for x in ["аренд" , "снять", "снимем", "заброни"]) and any(x in n for x in ["корпус", "домик"]):
        return "corpus"
    if "корпус" in n and any(x in n for x in ["свобод", "занят", "доступ"]):
        return "corpus"
    return None


def _looks_booking(n: str) -> bool:
    service = _service_from_text(n)
    if not service:
        return False
    # Mentioning an object is not enough. Information about price/conditions must stay in
    # the normal FAQ/handoff flow even if the sentence contains the word «аренда».
    informational = any(x in n for x in [
        "сколько стоит", "какая цена", "цена арен", "стоимость арен", "что входит",
        "какие условия", "условия арен", "расскажите про", "расскажите об",
    ])
    strong_markers = [
        "заброн", "бронь", "брониров", "снять", "снимем", "сниму",
        "свобод", "занят", "доступ", "хочу", "нужна бесед", "нужен корпус",
        "запис", "заказать", "оформить",
    ]
    if any(x in n for x in strong_markers):
        return True
    if "аренд" in n and not informational:
        return True
    if informational:
        return False
    # Terse first messages like «Беседка 12 октября в 14:00 на 2 часа» are also
    # clearly transactional even without a verb. Require a schedule clue so general
    # information/price questions are not hijacked.
    schedule_clue = re.search(
        r"(?:\bсегодня\b|\bзавтра\b|\bпослезавтра\b|"
        r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|"
        r"\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)|"
        r"\b(?:в|к|с)\s*\d{1,2}(?::\d{2})?\b)",
        n,
    )
    return bool(schedule_clue)



def _looks_existing_booking_management(n: str) -> bool:
    """Cancellation/change of an already existing booking is not a new booking flow.

    v5.9 does not yet have a safe lookup/edit flow for confirmed bookings, so these requests must
    fail closed to a manager instead of accidentally creating another reservation.
    """
    if not any(x in n for x in ["бесед", "газеб", "корпус", "домик"]):
        return False
    return any(x in n for x in [
        "отменить брон", "отмена брон", "отменить бронир", "отменить заявку",
        "изменить брон", "изменить бронир", "поменять брон", "перенести брон",
        "изменить время брон", "изменить дату брон",
    ])


def is_booking_request(text: str) -> bool:
    """Public predicate for fail-safe handoff when the booking backend is disabled/unavailable."""
    n = _norm(text)
    return _looks_booking(n) or _looks_existing_booking_management(n)

def _looks_unrelated_support_question(n: str) -> bool:
    """Known non-booking support topics that must not be swallowed by an active booking form."""
    if not n:
        return False
    # If the parent is still clearly talking about the booking object/slot, keep the booking flow.
    if _looks_booking(n):
        return False
    topic_markers = [
        "документ", "079", "снилс", "что взять", "что брать", "питани", "кормят",
        "сколько стоит", "какая цена", "стоимость", "что входит", "условия арен",
        "лекар", "таблет", "медпункт", "забол", "навещ", "посещ", "возраст",
        "сколько лет", "заезд", "выезд", "трансфер", "скид", "льгот",
        "кружк", "программ", "территори", "инфраструктур", "потерял", "потеряла",
        "забыл вещ", "забыла вещ", "вернуть путев", "возврат путев", "перенести ребенка",
        "поменять смен", "не проходит оплат", "не могу оплат", "обижают", "буллинг",
        "конфликт в отряде", "не могу дозвон", "связаться с ребен",
    ]
    if any(x in n for x in topic_markers):
        return True
    # Typical short FAQ questions with a question mark should escape only when they are not
    # recognizable booking data (date/time/duration/guest count/name/phone are handled by the form).
    return False


def _parse_phone(text: str) -> str | None:
    # Russian-style phone, tolerant to spaces, dashes and parentheses.
    m = re.search(r"(?<!\d)(?:\+?7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)", text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) != 11:
        return None
    if digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


def _parse_guest_count(n: str) -> int | None:
    m = re.search(r"\b(\d{1,3})\s*(?:человек|чел\.?|гостей|гостя|детей|взрослых)\b", n)
    if not m:
        return None
    value = int(m.group(1))
    return value if 1 <= value <= 999 else None


def _parse_duration_minutes(n: str) -> int | None:
    # "на 2 часа дня" means 14:00, not a two-hour duration. Iterate over
    # candidates and explicitly skip a duration-looking fragment followed by a daypart.
    for m in re.finditer(r"\bна\s+(\d{1,2})(?:[.,](\d))?\s*(час(?:а|ов)?|ч\.?)\b", n):
        tail = n[m.end():].lstrip()
        if re.match(r"^(?:дня|вечера|ночи|утра)\b", tail):
            continue
        hours = int(m.group(1))
        tenths = int(m.group(2) or 0)
        minutes = hours * 60 + tenths * 6
        return minutes if 30 <= minutes <= 24 * 60 else None
    m = re.search(r"\bна\s+(\d{2,4})\s*(?:минут|мин\.?)(?:\b|$)", n)
    if m:
        minutes = int(m.group(1))
        return minutes if 15 <= minutes <= 24 * 60 else None
    return None


def _parse_time(n: str) -> str | None:
    # 14:30. A bare 14.30 is ambiguous with a date, so dot-time requires "в/к".
    m = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", n)
    if m:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    m = re.search(r"\b(?:в|к)\s*([01]?\d|2[0-3])[.]([0-5]\d)(?!\d)", n)
    if m:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    # в 2 часа дня / к 7 вечера / в 10 утра
    m = re.search(r"\b(?:в|к|около|на)\s*(\d{1,2})(?:\s*час(?:а|ов)?)?\s*(утра|дня|вечера|ночи)\b", n)
    if m:
        hour = int(m.group(1))
        part = m.group(2)
        if not 1 <= hour <= 12:
            return None
        if part in {"дня", "вечера"} and hour < 12:
            hour += 12
        if part == "ночи" and hour == 12:
            hour = 0
        return f"{hour:02d}:00"
    # в 14 / к 14 часам
    m = re.search(r"\b(?:в|к)\s*([01]?\d|2[0-3])(?:\s*(?:час(?:а|ов|ам)?|ч\.?))?\b", n)
    if m:
        return f"{int(m.group(1)):02d}:00"
    return None


def _parse_time_range(n: str) -> tuple[str, int] | None:
    """Parse a same-day interval such as «с 14 до 17» or «с 14:30 до 18:00»."""
    m = re.search(
        r"\bс\s*([01]?\d|2[0-3])(?::([0-5]\d))?\s*(?:до|[-–—])\s*([01]?\d|2[0-3])(?::([0-5]\d))?\b",
        n,
    )
    if not m:
        return None
    h1, m1 = int(m.group(1)), int(m.group(2) or 0)
    h2, m2 = int(m.group(3)), int(m.group(4) or 0)
    start_minutes = h1 * 60 + m1
    end_minutes = h2 * 60 + m2
    if end_minutes <= start_minutes:
        return None
    return f"{h1:02d}:{m1:02d}", end_minutes - start_minutes


def _month_number(word: str) -> int | None:
    w = word.lower().replace("ё", "е")
    for stem, month in _MONTHS.items():
        if w.startswith(stem):
            return month
    return None


def _candidate_dates(n: str, *, today: date) -> tuple[list[date], str | None]:
    """Parse explicit/relative Russian dates. Returns dates and a validation error."""
    if re.search(r"\bпослезавтра\b", n):
        return [today + timedelta(days=2)], None
    if re.search(r"\bзавтра\b", n):
        return [today + timedelta(days=1)], None
    if re.search(r"\bсегодня\b", n):
        return [today], None

    found: list[date] = []
    # Ranges like "14–16 ноября" / "14-16 ноября 2026".
    month_words = "январ[ьяею]?|феврал[ьяею]?|марта?|апрел[ьяею]?|ма[йя]|июн[ьяею]?|июл[ьяею]?|август[аеу]?|сентябр[ьяею]?|октябр[ьяею]?|ноябр[ьяею]?|декабр[ьяею]?"
    rm = re.search(rf"\b(\d{{1,2}})\s*[-–—]\s*(\d{{1,2}})\s+({month_words})(?:\s+(\d{{4}}))?\b", n)
    if rm:
        d1, d2 = int(rm.group(1)), int(rm.group(2))
        mo = _month_number(rm.group(3)); y = int(rm.group(4) or today.year)
        if mo:
            try:
                a = date(y, mo, d1); b = date(y, mo, d2)
            except ValueError:
                return [], f"Такого диапазона дат нет: {rm.group(0)}."
            if not rm.group(4) and b < today:
                a = a.replace(year=today.year + 1); b = b.replace(year=today.year + 1)
            return [a, b], None

    # dd.mm[.yyyy]
    for m in re.finditer(r"(?<!\d)(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?(?!\d)", n):
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        if y < 100:
            y += 2000
        try:
            parsed = date(y, mo, d)
        except ValueError:
            return [], f"Такой даты нет: {d:02d}.{mo:02d}.{y}."
        if not m.group(3) and parsed < today:
            try:
                parsed = parsed.replace(year=today.year + 1)
            except ValueError:
                pass
        found.append(parsed)

    # 31 сентября [2026]
    for m in re.finditer(rf"\b(\d{{1,2}})\s+({month_words})(?:\s+(\d{{4}}))?\b", n):
        d = int(m.group(1)); mo = _month_number(m.group(2)); y = int(m.group(3) or today.year)
        if not mo:
            continue
        try:
            parsed = date(y, mo, d)
        except ValueError:
            return [], f"Такой даты нет: {d} {m.group(2)} {y}."
        if not m.group(3) and parsed < today:
            parsed = parsed.replace(year=today.year + 1)
        if parsed not in found:
            found.append(parsed)
    return found, None


def _format_ru_date(iso_date: str) -> str:
    d = date.fromisoformat(iso_date)
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    return f"{d.day} {months[d.month - 1]} {d.year}"


def _looks_confirmation(n: str) -> bool:
    return n in {"да", "да, все верно", "да все верно", "верно", "подтверждаю", "подтвердить", "оформить", "все правильно", "всё правильно"}


def _looks_cancel(n: str) -> bool:
    return n in {"нет", "отмена", "отменить", "не надо", "передумал", "передумала", "отменить заявку", "отменить бронь"}


class BookingEngine:
    """Strict state machine for parent booking requests.

    AI is not allowed to decide availability. Every slot decision is delegated to the
    booking backend, which owns the shared Google Sheet calendar.
    """

    def __init__(self, settings: Settings, db: Database, backend: BookingBackend | None = None):
        self.settings = settings
        self.db = db
        self.tz = ZoneInfo(settings.booking_timezone)
        self.backend: BookingBackend = backend or AppsScriptBookingBackend(settings)

    async def close(self) -> None:
        await self.backend.close()

    async def health(self) -> dict[str, Any]:
        result = await self.backend.call("health", {})
        if result.get("healthy") is not True:
            raise BookingAPIError("booking backend health check failed")
        return result

    def blocks_other_flow(self, user_id: int) -> bool:
        """Whether an unfinished local booking form would steal answers from another workflow."""
        row = self.db.get_booking_session(user_id=user_id)
        return bool(row and str(row.get("status") or "") != "pending_manager")

    async def abandon_for_other_flow(self, user_id: int, *, reason: str = "switched_to_other_flow") -> None:
        """Release a temporary hold and clear only an unfinished booking form.

        A booking already submitted to a manager is intentionally kept: its candidate() method
        does not consume unrelated support answers, so it can safely coexist with another ticket.
        """
        row = self.db.get_booking_session(user_id=user_id)
        if not row or str(row.get("status") or "") == "pending_manager":
            return
        payload = dict(row.get("payload") or {})
        await self._cancel_external_if_any(payload, reason=reason)
        self.db.clear_booking_session(user_id=user_id)

    def candidate(self, text: str, *, user_id: int) -> bool:
        n = _norm(text)
        session = self.db.get_booking_session(user_id=user_id)
        if not session:
            return _looks_booking(n) or _looks_existing_booking_management(n)
        if str(session.get("status") or "") == "pending_manager":
            # A pending request may live for hours. Do not hijack unrelated FAQ questions
            # while the parent waits for a manager decision.
            status_question = ("заявк" in n or "брон" in n) and any(
                x in n for x in ["моя", "моей", "мою", "статус", "что с", "что по"]
            )
            return _looks_cancel(n) or _looks_booking(n) or status_question
        # While collecting a booking, let strong unrelated FAQ/support topics pass to the normal
        # router (or Smart Handoff) instead of answering them as if they were a date/phone/name.
        if _looks_unrelated_support_question(n):
            return False
        return True

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _today(self) -> date:
        return self._now().date()

    async def _service_info(self, payload: dict[str, Any]) -> dict[str, Any]:
        cached = payload.get("service_info")
        if isinstance(cached, dict) and cached.get("service_key") == payload.get("service_key"):
            return cached
        result = await self.backend.call("get_service", {"service_key": payload.get("service_key")})
        if not result.get("found") or not result.get("enabled"):
            raise BookingAPIError("service is not configured or disabled")
        payload["service_info"] = result
        return result

    def _save(self, user: BookingUser, step: str, payload: dict[str, Any], *, status: str = "active") -> None:
        self.db.save_booking_session(
            user_id=user.user_id, peer_id=user.peer_id, step=step, status=status,
            payload=payload, external_booking_id=str(payload.get("booking_id") or "") or None,
            ttl_minutes=max(self.settings.booking_hold_minutes * 4, 60), platform="vk",
        )

    async def _cancel_external_if_any(self, payload: dict[str, Any], *, reason: str) -> None:
        booking_id = str(payload.get("booking_id") or "")
        if not booking_id:
            return
        try:
            await self.backend.call("cancel_booking", {"booking_id": booking_id, "reason": reason})
        except Exception:
            pass

    async def handle_text(self, user: BookingUser, text: str) -> BookingResult:
        n = _norm(text)
        row = self.db.get_booking_session(user_id=user.user_id)
        payload: dict[str, Any] = dict(row.get("payload") or {}) if row else {}
        step = str(row.get("step") or "") if row else ""
        status = str(row.get("status") or "") if row else ""
        # Remember the exact slot/party that an existing Google hold represents.
        # If the parent changes any of these details before confirming, the old hold
        # must be cancelled and re-created. Otherwise the confirmation summary could
        # describe one slot while Google still holds another one.
        held_fields = ("service_key", "start_date", "end_date", "start_time", "duration_minutes", "guest_count")
        held_signature = tuple(payload.get(k) for k in held_fields) if payload.get("booking_id") else None

        # A request to cancel/reschedule an already existing booking must never start a second
        # reservation. Confirmed-booking self-service is intentionally not implemented in v5.9/v6.0.
        if not row and _looks_existing_booking_management(n):
            return BookingResult(True, state="escalate", error_reason="booking_management_requires_manager")

        if not row and not _looks_booking(n):
            return BookingResult(False)

        if _looks_cancel(n):
            await self._cancel_external_if_any(payload, reason="cancelled_by_parent")
            self.db.clear_booking_session(user_id=user.user_id)
            return BookingResult(True, "Хорошо, заявку на бронирование отменил. Если захотите выбрать другое время — просто напишите 😊", state="cancelled")

        if status == "pending_manager":
            booking_id = str(payload.get("booking_id") or "") or None
            return BookingResult(
                True,
                "Ваша заявка уже передана менеджеру и ожидает подтверждения 💛\n\nКак только сотрудник подтвердит или отклонит её, я напишу сюда.",
                state="pending_manager", booking_id=booking_id,
            )

        if step == "confirm_parent" and _looks_confirmation(n):
            booking_id = str(payload.get("booking_id") or "")
            if not booking_id:
                self.db.clear_booking_session(user_id=user.user_id)
                return BookingResult(True, "Не получилось найти временную бронь. Давайте начнём заново — напишите услугу и дату.", state="restart")
            try:
                result = await self.backend.call("submit_booking", {
                    "booking_id": booking_id,
                    "full_name": payload.get("full_name"),
                    "phone": payload.get("phone"),
                    "guest_count": payload.get("guest_count"),
                    "comment": payload.get("comment", ""),
                })
            except BookingAPIError:
                return BookingResult(True, state="escalate", error_reason="booking_backend_unavailable")
            if not result.get("submitted"):
                self.db.clear_booking_session(user_id=user.user_id)
                return BookingResult(True, result.get("message") or "Временная бронь уже истекла. Напишите дату и время ещё раз — я перепроверю доступность.", state="expired")
            payload["status"] = "pending_manager"
            self._save(user, "pending_manager", payload, status="pending_manager")
            service = payload.get("service_info") or {}
            start_text = self._period_text(payload, service)
            manager_text = (
                f"📅 Новая заявка на бронирование\n\n"
                f"ID: {booking_id}\n"
                f"Услуга: {service.get('service_name') or payload.get('service_key')}\n"
                f"Когда: {start_text}\n"
                f"Гостей: {payload.get('guest_count')}\n"
                f"ФИО: {payload.get('full_name')}\n"
                f"Телефон: {payload.get('phone')}\n"
                f"VK: https://vk.ru/{user.username}" if user.username else
                f"📅 Новая заявка на бронирование\n\nID: {booking_id}\nУслуга: {service.get('service_name') or payload.get('service_key')}\nКогда: {start_text}\nГостей: {payload.get('guest_count')}\nФИО: {payload.get('full_name')}\nТелефон: {payload.get('phone')}\nVK: https://vk.ru/id{user.user_id}"
            )
            return BookingResult(
                True,
                "✅ Заявка создана и передана менеджеру.\n\nПока статус — «ожидает подтверждения». Как только сотрудник подтвердит бронь, я сообщу Вам здесь.",
                state="pending_manager", booking_id=booking_id, manager_text=manager_text,
            )

        # Merge fields from the latest parent message.
        service_key = _service_from_text(n)
        if service_key:
            if payload.get("service_key") and payload.get("service_key") != service_key and payload.get("booking_id"):
                await self._cancel_external_if_any(payload, reason="parent_changed_service")
                payload = {}
            payload["service_key"] = service_key

        # Phone numbers contain dashes and can accidentally resemble dates (e.g. 67-89).
        # Remove the recognized phone span before date parsing so contact details never
        # overwrite or invalidate an already selected booking date.
        phone = _parse_phone(text)
        date_text = n
        if phone:
            date_text = re.sub(
                r"(?<!\d)(?:\+?7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)",
                " ",
                date_text,
            )
        dates, date_error = _candidate_dates(date_text, today=self._today())
        if date_error:
            self._save(user, "date", payload)
            return BookingResult(True, date_error + " Подскажите, пожалуйста, правильную дату.", state="collecting")
        if dates:
            if len(dates) > 1:
                payload["start_date"] = dates[0].isoformat()
                payload["end_date"] = dates[1].isoformat()
            elif step == "end_date" and payload.get("start_date"):
                # In the two-message corpus flow, the follow-up single date is the
                # inclusive end date, not a replacement for the already collected start.
                payload["end_date"] = dates[0].isoformat()
            else:
                payload["start_date"] = dates[0].isoformat()

        time_range = _parse_time_range(n)
        if time_range:
            payload["start_time"], payload["duration_minutes"] = time_range
        else:
            parsed_time = _parse_time(n)
            if parsed_time:
                payload["start_time"] = parsed_time
            duration = _parse_duration_minutes(n)
            if duration:
                payload["duration_minutes"] = duration
        guests = _parse_guest_count(n)
        if guests:
            payload["guest_count"] = guests
        if phone:
            payload["phone"] = phone

        # Stage-specific plain-text capture.
        if step == "full_name" and not payload.get("full_name"):
            candidate = re.sub(r"\s+", " ", text.strip())
            if 2 <= len(candidate.split()) <= 5 and all(re.fullmatch(r"[A-Za-zА-Яа-яЁё\-]+", x) for x in candidate.split()):
                payload["full_name"] = candidate
        if step == "guest_count" and not payload.get("guest_count") and re.fullmatch(r"\d{1,3}", n):
            payload["guest_count"] = int(n)
        if step == "duration" and not payload.get("duration_minutes"):
            m = re.fullmatch(r"(\d{1,2})(?:\s*(?:час(?:а|ов)?|ч))?", n)
            if m:
                payload["duration_minutes"] = int(m.group(1)) * 60
        if step == "time" and not payload.get("start_time"):
            m = re.fullmatch(r"([01]?\d|2[0-3])(?::([0-5]\d))?", n)
            if m:
                payload["start_time"] = f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"

        # A hold is tied to one exact service/slot/party size. If the parent edits
        # any of those values while we are collecting contact details, release the
        # old hold and let the normal validation path create a fresh one.
        if held_signature is not None and payload.get("booking_id"):
            current_signature = tuple(payload.get(k) for k in held_fields)
            if current_signature != held_signature:
                await self._cancel_external_if_any(payload, reason="parent_changed_booking_details")
                for key in ("booking_id", "hold_expires_at", "resource_name", "status"):
                    payload.pop(key, None)

        if not payload.get("service_key"):
            self._save(user, "service", payload)
            return BookingResult(True, "Что хотите забронировать — беседку или аренду корпуса?", state="collecting")

        try:
            service = await self._service_info(payload)
        except BookingAPIError:
            return BookingResult(True, state="escalate", error_reason="booking_service_not_configured")

        mode = str(service.get("mode") or "hourly")
        if not payload.get("start_date"):
            self._save(user, "date", payload)
            return BookingResult(True, "На какую дату хотите забронировать? Напишите, например: «12 октября». ", state="collecting")

        start_date = date.fromisoformat(str(payload["start_date"]))
        if start_date < self._today():
            payload.pop("start_date", None)
            self._save(user, "date", payload)
            return BookingResult(True, "Эта дата уже прошла. Подскажите, пожалуйста, будущую дату.", state="collecting")

        if mode == "date_range":
            if not payload.get("end_date"):
                self._save(user, "end_date", payload)
                return BookingResult(True, f"С какого дня понял: {_format_ru_date(payload['start_date'])}. До какого числа нужен корпус?", state="collecting")
            if date.fromisoformat(str(payload["end_date"])) < start_date:
                payload.pop("end_date", None)
                self._save(user, "end_date", payload)
                return BookingResult(True, "Дата окончания получилась раньше даты начала. Напишите, пожалуйста, дату окончания ещё раз.", state="collecting")
        else:
            if not payload.get("start_time"):
                self._save(user, "time", payload)
                return BookingResult(True, f"На {_format_ru_date(payload['start_date'])} какое время нужно? Например: «14:00». ", state="collecting")
            if not payload.get("duration_minutes"):
                self._save(user, "duration", payload)
                return BookingResult(True, "На сколько часов нужна беседка?", state="collecting")

            # Never create a hold for a time that has already passed today.
            hh, mm = map(int, str(payload["start_time"]).split(":"))
            requested_start = datetime.combine(start_date, time(hh, mm), self.tz)
            if requested_start <= self._now():
                payload.pop("start_time", None)
                payload.pop("duration_minutes", None)
                self._save(user, "time", payload)
                return BookingResult(
                    True,
                    "Это время уже прошло. Подскажите, пожалуйста, другое время сегодня или будущую дату.",
                    state="collecting",
                )

        # Guest count is part of availability because different resources may have
        # different capacities. Never reserve a resource before this is known.
        max_guests = int(service.get("max_guests") or 0)
        if not payload.get("guest_count"):
            self._save(user, "guest_count", payload)
            return BookingResult(True, "Сколько примерно будет человек?", state="collecting")
        if int(payload["guest_count"]) <= 0 or (max_guests and int(payload["guest_count"]) > max_guests):
            payload.pop("guest_count", None)
            self._save(user, "guest_count", payload)
            limit = f" Максимум для этой услуги — {max_guests} человек." if max_guests else ""
            return BookingResult(True, "Проверьте, пожалуйста, количество гостей." + limit, state="collecting")

        # Make a real hold only after the complete slot and party size are known.
        if not payload.get("booking_id"):
            interval = self._interval_payload(payload, service)
            try:
                hold = await self.backend.call("create_hold", {
                    **interval,
                    "service_key": payload["service_key"],
                    "guest_count": int(payload["guest_count"]),
                    "vk_user_id": user.user_id,
                    "vk_peer_id": user.peer_id,
                    "ttl_minutes": self.settings.booking_hold_minutes,
                })
            except BookingAPIError:
                return BookingResult(True, state="escalate", error_reason="booking_backend_unavailable")
            if not hold.get("available"):
                reason = str(hold.get("reason") or "occupied")
                if mode == "date_range":
                    payload.pop("end_date", None)
                    self._save(user, "end_date", payload)
                else:
                    payload.pop("start_time", None)
                    payload.pop("duration_minutes", None)
                    self._save(user, "time", payload)
                alternatives = hold.get("alternatives") or []
                extra = ""
                if isinstance(alternatives, list) and alternatives:
                    extra = "\n\nСвободные варианты: " + ", ".join(str(x) for x in alternatives[:4])
                if reason == "capacity":
                    message = "Для такого количества гостей сейчас нет подходящего свободного объекта."
                elif reason == "past":
                    message = "Это время уже прошло."
                elif reason in {"before_open", "after_close", "too_short", "too_long", "crosses_day"}:
                    message = "Выбранный интервал не подходит под правила аренды этого объекта."
                else:
                    message = "К сожалению, выбранное время уже занято."
                return BookingResult(True, message + extra + "\n\nНапишите другое время/дату — я сразу перепроверю.", state="unavailable")
            payload["booking_id"] = str(hold.get("booking_id") or "")
            payload["hold_expires_at"] = hold.get("expires_at")
            if hold.get("resource_name"):
                payload["resource_name"] = hold.get("resource_name")

        if not payload.get("full_name"):
            self._save(user, "full_name", payload)
            return BookingResult(True, "Напишите, пожалуйста, ФИО человека, на которого оформляем заявку.", state="collecting")

        if not payload.get("phone"):
            self._save(user, "phone", payload)
            return BookingResult(True, "И номер телефона для связи, пожалуйста. Например: +7 912 345-67-89", state="collecting")

        self._save(user, "confirm_parent", payload)
        period = self._period_text(payload, service)
        service_name = service.get("service_name") or payload.get("service_key")
        resource = payload.get("resource_name") or service.get("resource_name") or ""
        resource_line = f"\n📍 {resource}" if resource else ""
        return BookingResult(
            True,
            f"Проверьте заявку, пожалуйста:\n\n"
            f"✨ {service_name}{resource_line}\n"
            f"📅 {period}\n"
            f"👥 {payload['guest_count']} человек\n"
            f"👤 {payload['full_name']}\n"
            f"📞 {payload['phone']}\n\n"
            "Если всё верно — подтвердите заявку. До подтверждения менеджером это ещё не окончательная бронь.",
            state="confirm_parent", booking_id=str(payload.get("booking_id") or ""),
        )

    def _interval_payload(self, payload: dict[str, Any], service: dict[str, Any]) -> dict[str, str]:
        mode = str(service.get("mode") or "hourly")
        if mode == "date_range":
            start = datetime.combine(date.fromisoformat(payload["start_date"]), time.min, self.tz)
            # End date is inclusive for human UX; store exclusive next-day boundary.
            end = datetime.combine(date.fromisoformat(payload["end_date"]) + timedelta(days=1), time.min, self.tz)
        else:
            hh, mm = map(int, str(payload["start_time"]).split(":"))
            start = datetime.combine(date.fromisoformat(payload["start_date"]), time(hh, mm), self.tz)
            end = start + timedelta(minutes=int(payload["duration_minutes"]))
        return {"start_at": start.isoformat(), "end_at": end.isoformat()}

    def _period_text(self, payload: dict[str, Any], service: dict[str, Any]) -> str:
        mode = str(service.get("mode") or "hourly")
        if mode == "date_range":
            return f"{_format_ru_date(payload['start_date'])} — {_format_ru_date(payload['end_date'])}"
        start = str(payload.get("start_time") or "")
        duration = int(payload.get("duration_minutes") or 0)
        hh, mm = map(int, start.split(":"))
        dt = datetime.combine(date.fromisoformat(payload["start_date"]), time(hh, mm)) + timedelta(minutes=duration)
        return f"{_format_ru_date(payload['start_date'])}, {start}–{dt.strftime('%H:%M')}"

    async def parent_decision(self, user: BookingUser, *, confirm: bool) -> BookingResult:
        # Callback buttons can remain visible in old VK messages even after a booking
        # was already confirmed/rejected. Never pretend that such a stale click changed state.
        if self.db.get_booking_session(user_id=user.user_id) is None:
            return BookingResult(
                True,
                "Эта заявка уже не ожидает Вашего подтверждения. Если нужна новая бронь — напишите дату и услугу заново.",
                state="stale",
            )
        return await self.handle_text(user, "подтверждаю" if confirm else "отмена")

    async def manager_decision(self, *, booking_id: str, manager_id: int, approve: bool) -> dict[str, Any]:
        decision = "confirmed" if approve else "rejected"
        result = await self.backend.call("manager_decision", {
            "booking_id": booking_id,
            "decision": decision,
            "manager_id": manager_id,
        })
        if not result.get("updated"):
            raise BookingAPIError(str(result.get("message") or "booking was not updated"))
        user_id = int(result.get("vk_user_id") or 0)
        peer_id = int(result.get("vk_peer_id") or 0)
        if user_id:
            self.db.clear_booking_session(user_id=user_id)
        return {**result, "peer_id": peer_id, "user_id": user_id, "decision": decision}
