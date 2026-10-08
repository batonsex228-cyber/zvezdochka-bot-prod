from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .booking import BookingAPIError, BookingEngine, BookingResult, BookingUser, is_booking_request, public_rental_info
from .shift_booking import ShiftEngine, ShiftUser, is_shift_application_request
from .config import Settings
from .core import SupportCore
from .conversation import (
    attachment_intro, classify_social_only, greeting_intro, is_greeting_only,
    social_reply, split_leading_greeting, with_greeting,
)
from .db import Database
from .intent_router import IntentDecision, route
from .intake import (
    IntakeResult, IntakeUser, SmartHandoffEngine, build_urgent_submission,
    contains_payment_secret, is_urgent_safety, public_event_info, classify_transaction,
)
from .knowledge_loop import knowledge_candidate_allowed
from .operator import OperatorBridge
from .responder import BotAnswer, manual_knowledge_allowed
from .texts import ESCALATED, ESCALATED_NO_CHANNEL, WELCOME, escalation_text
from .version import VERSION
from .vk_format import html_to_vk_text
from .vk_keyboards import (
    admin_keyboard,
    answer_keyboard,
    clarification_keyboard,
    fallback_keyboard,
    main_keyboard,
    manager_ticket_keyboard,
    manager_booking_keyboard,
    manager_shift_keyboard,
    parent_booking_confirmation_keyboard,
    parent_intake_confirmation_keyboard,
    knowledge_candidate_keyboard,
    newsletter_preview_keyboard,
    notification_keyboard,
    shifts_carousel_template,
    knowledge_menu_keyboard,
    knowledge_preview_keyboard,
    knowledge_ttl_keyboard,
)


class VKAPIError(RuntimeError):
    def __init__(self, method: str, code: int | None, message: str):
        self.method = method
        self.code = code
        super().__init__(f"VK API {method}: [{code}] {message}")


@dataclass(frozen=True)
class VKUser:
    user_id: int
    peer_id: int
    full_name: str
    username: str | None


class VKAdapter:
    """VK community bot using Bots Long Poll. No Telegram dependency."""

    API_BASE = "https://api.vk.com/method/"
    TYPING_REFRESH_SECONDS = 4.0

    def __init__(self, settings: Settings, core: SupportCore, db: Database, operator: OperatorBridge, *, knowledge_base=None, booking: BookingEngine | None = None, intake: SmartHandoffEngine | None = None, shifts: ShiftEngine | None = None):
        if not settings.vk_group_token or not settings.vk_group_id:
            raise RuntimeError("VKAdapter requires VK_GROUP_TOKEN and VK_GROUP_ID")
        self.settings = settings
        self.core = core
        self.db = db
        self.operator = operator
        self.kb = knowledge_base
        self.booking = booking
        self.intake = intake
        self.shifts = shifts
        self._shift_sync_task: asyncio.Task | None = None
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=12.0))
        self._stopping = False
        self.group_name: str | None = None
        self.source_group_id: int | None = None
        self._profile_pages: list[dict[str, Any]] = []
        self._wall_pages: list[dict[str, Any]] = []

    async def close(self) -> None:
        self._stopping = True
        if self._shift_sync_task is not None and not self._shift_sync_task.done():
            self._shift_sync_task.cancel()
            try:
                await self._shift_sync_task
            except asyncio.CancelledError:
                pass
        await self.client.aclose()
        if self.booking is not None:
            await self.booking.close()
        elif self.shifts is not None and self.shifts.backend is not None:
            await self.shifts.backend.close()

    @staticmethod
    def _encode_param(value: Any) -> Any:
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (list, tuple, set)):
            return ",".join(str(x) for x in value)
        return value

    async def api(self, method: str, *, token: str | None = None, **params: Any) -> Any:
        data = {
            "access_token": token or self.settings.vk_group_token,
            "v": self.settings.vk_api_version,
            **{k: self._encode_param(v) for k, v in params.items() if v is not None},
        }
        response = await self.client.post(self.API_BASE + method, data=data)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            err = payload.get("error") or {}
            raise VKAPIError(method, err.get("error_code"), str(err.get("error_msg", "unknown error")))
        return payload.get("response")

    async def validate(self) -> dict[str, Any]:
        server = await self.api("groups.getLongPollServer", group_id=self.settings.vk_group_id)
        if not isinstance(server, dict) or not server.get("server") or not server.get("key"):
            raise RuntimeError("VK не вернул сервер Long Poll. Проверьте настройки сообщества.")

        lp_settings = await self.api("groups.getLongPollSettings", group_id=self.settings.vk_group_id)
        if isinstance(lp_settings, dict):
            events = lp_settings.get("events") or {}
            if not events.get("message_new"):
                raise RuntimeError("В Long Poll API выключено событие message_new")
            if not events.get("message_event"):
                raise RuntimeError("В Long Poll API выключено событие message_event — без него не работают callback-кнопки")

        try:
            group = await self.api("groups.getById", group_id=self.settings.vk_group_id)
            groups = (group.get("groups") or []) if isinstance(group, dict) else (group or [])
            if groups and isinstance(groups[0], dict):
                self.group_name = str(groups[0].get("name") or "") or None
        except Exception as exc:
            print(f"[vk] groups.getById warning: {exc}", flush=True)

        return server

    async def manager_inbox_allowed(self) -> bool | None:
        manager_id = int(self.settings.vk_manager_user_id or 0)
        if manager_id <= 0:
            return None
        response = await self.api(
            "messages.isMessagesFromGroupAllowed",
            group_id=self.settings.vk_group_id,
            user_id=manager_id,
        )
        if isinstance(response, dict):
            return bool(response.get("is_allowed"))
        return None

    async def send_message(
        self,
        peer_id: int,
        text: str,
        *,
        keyboard: str | None = None,
        template: str | None = None,
        intent: str | None = None,
    ) -> int:
        params: dict[str, Any] = {
            "peer_id": peer_id,
            "random_id": random.randint(1, 2_000_000_000),
            "message": text,
        }
        if keyboard:
            params["keyboard"] = keyboard
        if template:
            params["template"] = template
        if intent:
            params["intent"] = intent
        try:
            response = await self.api("messages.send", **params)
        except VKAPIError as exc:
            # VK may reject the optional messages.send intent with error 943
            # ("Cannot use this intent") for communities where that intent is not available.
            # Regular support replies do not need an intent, so retry safely without it.
            if exc.code == 943 and "intent" in params:
                params.pop("intent", None)
                response = await self.api("messages.send", **params)
            # Error 100 can also be caused by optional presentation parameters.
            # Fall back to a plain message, but never retry rate/access errors.
            elif exc.code == 100 and ("intent" in params or "template" in params):
                params.pop("intent", None)
                params.pop("template", None)
                response = await self.api("messages.send", **params)
            else:
                raise
        if isinstance(response, dict):
            for key in ("message_id", "conversation_message_id", "id"):
                if response.get(key) is not None:
                    return int(response[key])
            return 0
        return int(response or 0)

    async def send_plain_from_operator(self, peer_id: int, text: str) -> None:
        await self.send_message(peer_id, text, keyboard=main_keyboard())

    async def send_to_manager(self, text: str, ticket_id: int | None = None) -> int:
        manager_id = int(self.settings.vk_manager_user_id or 0)
        if manager_id <= 0:
            raise RuntimeError("VK_MANAGER_USER_ID is not configured")
        keyboard = manager_ticket_keyboard(ticket_id) if ticket_id else admin_keyboard()
        return await self.send_message(manager_id, text, keyboard=keyboard)

    async def mark_support_conversation(self, peer_id: int, needs_human: bool) -> None:
        # VK API 5.199: answered=False means unanswered; important=True adds the star.
        await self.api(
            "messages.markAsAnsweredConversation",
            peer_id=peer_id,
            answered=not needs_human,
            group_id=self.settings.vk_group_id,
        )
        await self.api(
            "messages.markAsImportantConversation",
            peer_id=peer_id,
            important=needs_human,
            group_id=self.settings.vk_group_id,
        )

    async def typing(self, peer_id: int) -> None:
        try:
            await self.api(
                "messages.setActivity",
                peer_id=peer_id,
                type="typing",
                group_id=self.settings.vk_group_id,
            )
        except Exception:
            pass

    async def _typing_keepalive(self, peer_id: int) -> None:
        """Refresh VK's short-lived "typing" activity while a handler is busy.

        Google Booking calls can take several seconds.  A single setActivity call may
        disappear before the reply is ready, so refresh it every four seconds until
        the whole incoming update finishes.
        """
        try:
            while True:
                await self.typing(peer_id)
                await asyncio.sleep(self.TYPING_REFRESH_SECONDS)
        except asyncio.CancelledError:
            return

    @staticmethod
    def _update_peer_id(update: dict[str, Any]) -> int:
        obj = update.get("object") or {}
        if not isinstance(obj, dict):
            return 0
        if str(update.get("type") or "") == "message_new":
            message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
            return int(message.get("peer_id") or message.get("from_id") or 0)
        return int(obj.get("peer_id") or 0)

    async def _event_answer(self, obj: dict[str, Any], text: str) -> None:
        event_id, user_id, peer_id = obj.get("event_id"), obj.get("user_id"), obj.get("peer_id")
        if not event_id or not user_id or not peer_id:
            return
        try:
            await self.api(
                "messages.sendMessageEventAnswer",
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
                event_data=json.dumps({"type": "show_snackbar", "text": text}, ensure_ascii=False),
            )
        except Exception as exc:
            print(f"[vk] event answer warning: {exc}", flush=True)

    @staticmethod
    def _decode_payload(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    async def _user(self, user_id: int, peer_id: int) -> VKUser:
        full_name = f"VK ID {user_id}"
        username: str | None = None
        try:
            rows = await self.api("users.get", user_ids=user_id, fields="screen_name")
            if rows and isinstance(rows, list) and isinstance(rows[0], dict):
                row = rows[0]
                full_name = " ".join(x for x in [str(row.get("first_name", "")), str(row.get("last_name", ""))] if x).strip() or full_name
                username = str(row.get("screen_name") or "") or None
        except Exception as exc:
            print(f"[vk] users.get failed for {user_id}: {exc}", flush=True)
        return VKUser(user_id=user_id, peer_id=peer_id, full_name=full_name, username=username)

    async def _welcome(self, user: VKUser) -> None:
        self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="start", name="start")
        await self.send_message(user.peer_id, html_to_vk_text(WELCOME), keyboard=main_keyboard())

    def _context_for_decision(self, user_id: int) -> dict[str, str] | None:
        return self.db.get_context(user_id=user_id, platform="vk")

    def _remember(self, user_id: int, decision: IntentDecision) -> None:
        if decision.entity:
            topic = "shift" if decision.entity.startswith("shift:") else ("winter_shift" if decision.entity == "winter" else decision.intent)
            self.db.set_context(
                user_id=user_id,
                topic=topic,
                entity=decision.entity,
                ttl_minutes=self.settings.context_ttl_minutes,
                platform="vk",
            )
        elif decision.intent in {"documents", "contract_documents", "phone_policy", "ndfl_documents", "arrival", "food", "contacts", "bring", "extra_services"}:
            self.db.set_context(
                user_id=user_id,
                topic=decision.intent,
                entity=None,
                ttl_minutes=self.settings.context_ttl_minutes,
                platform="vk",
            )

    async def _show_notifications(self, user: VKUser) -> None:
        subscribed = self.db.is_subscribed(user_id=user.user_id)
        if subscribed:
            text = "🔔 Важные уведомления включены.\n\nВы будете получать значимые объявления лагеря. Отключить можно в любой момент."
        else:
            text = "🔔 Хотите получать важные объявления лагеря — новые смены, изменения дат и информацию о заезде?\n\nПодписка добровольная, отключить её можно в любой момент."
        await self.send_message(user.peer_id, text, keyboard=notification_keyboard(subscribed))

    async def _clarify(self, user: VKUser, kind: str) -> None:
        if kind == "address":
            await self.send_message(
                user.peer_id,
                "📍 Уточните, пожалуйста, какой адрес нужен:",
                keyboard=clarification_keyboard("address"),
            )

    async def _process_question(self, *, text: str, intent: str | None, user: VKUser, greeting: str | None = None) -> None:
        if intent == "ask":
            await self.send_message(user.peer_id, "Задавайте 😊\n\nНапишите вопрос своими словами — постараюсь помочь.", keyboard=main_keyboard())
            return
        if intent == "notifications":
            await self._show_notifications(user)
            return

        decision = IntentDecision(intent=intent, confidence=1.0) if intent else route(text, self._context_for_decision(user.user_id))
        if decision.clarification:
            self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="clarification", name=decision.clarification)
            await self._clarify(user, decision.clarification)
            return
        resolved_intent = decision.intent
        self._remember(user.user_id, decision)
        core_intent = "dynamic_availability" if decision.entity in {"extra_service_dynamic", "current_availability"} else resolved_intent

        print(f"[vk] message: user_id={user.user_id} intent={resolved_intent or '-'}", flush=True)
        result = await self.core.process(text, intent=core_intent)
        answer = result.answer
        print(f"[vk] decision: user_id={user.user_id} supported={answer.supported} reason={answer.reason} faq_id={answer.faq_id or '-'}", flush=True)

        if result.supported and result.presented:
            event_id = self.db.log_answer(
                platform="vk", user_id=user.user_id, chat_id=user.peer_id, username=user.username,
                full_name=user.full_name, question=text, faq_id=answer.faq_id, reason=answer.reason,
                confidence=answer.confidence,
            )
            self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="auto_answer", name=answer.faq_id or resolved_intent or "faq")
            keyboard = answer_keyboard(answer.faq_id, event_id)
            # Main shifts output gets a native carousel. If VK rejects template, send_message falls back to text.
            template = shifts_carousel_template() if resolved_intent == "shifts_prices" else None
            await self.send_message(
                user.peer_id,
                html_to_vk_text(with_greeting(result.presented.text, greeting)),
                keyboard=keyboard,
                template=template,
            )
            try:
                await self.mark_support_conversation(user.peer_id, False)
            except Exception:
                pass
            return

        ticket_id = await self.operator.escalate(
            platform="vk", user_id=user.user_id, chat_id=user.peer_id, username=user.username,
            full_name=user.full_name, question=text, answer=answer,
        )
        self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="escalation", name=answer.reason or "unknown")
        customer_handoff = escalation_text(text, answer.reason, delivered=bool(ticket_id))
        if ticket_id:
            await self.send_message(user.peer_id, html_to_vk_text(with_greeting(customer_handoff, greeting)), keyboard=main_keyboard())
        else:
            await self.send_message(user.peer_id, html_to_vk_text(with_greeting(customer_handoff, greeting)), keyboard=fallback_keyboard())

    async def _try_shift_text(self, user: VKUser, text: str) -> bool:
        if self.shifts is None or not self.shifts.candidate(text, user_id=user.user_id):
            return False
        # Switching explicitly to a different booking service releases the unfinished shift form.
        if self.shifts.has_active(user.user_id) and is_booking_request(text) and not is_shift_application_request(text):
            self.shifts.abandon(user.user_id)
            return False
        if self.intake is not None and self.intake.has_active(user.user_id):
            self.intake.abandon_for_other_flow(user.user_id)
        if self.booking is not None and self.booking.blocks_other_flow(user.user_id):
            await self.booking.abandon_for_other_flow(user.user_id, reason="switched_to_shift_application")
        result = await self.shifts.handle_text(ShiftUser(user.user_id,user.peer_id), text)
        if not result.handled:
            return False
        if result.text:
            await self.send_message(user.peer_id, result.text, keyboard=main_keyboard())
        if result.application_id and result.manager_text:
            try:
                await self.send_message(int(self.settings.vk_manager_user_id), result.manager_text,
                                        keyboard=manager_shift_keyboard(result.application_id))
            except Exception as exc:
                print(f"[shift] manager notification failed: {type(exc).__name__}",flush=True)
        return True

    async def _send_booking_result(self, user: VKUser, result: BookingResult, *, original_question: str | None = None) -> bool:
        if not result.handled:
            return False
        if result.state == "escalate":
            # If the automatic calendar for accommodation is not configured yet, do not
            # make the parent hit a dead end. Collect a short structured request instead;
            # the manager will confirm dates manually.
            if self.intake is not None and original_question and result.error_reason in {"booking_service_not_configured", "booking_not_enabled"}:
                if classify_transaction(original_question) == "corpus_request":
                    self.db.clear_booking_session(user_id=user.user_id)
                    intake_result = await self.intake.handle_text(
                        IntakeUser(user.user_id, user.peer_id, user.full_name, user.username), original_question
                    )
                    return await self._send_intake_result(user, intake_result)
            answer = BotAnswer(False, None, confidence=0.0, reason=result.error_reason or "booking_backend_unavailable")
            ticket_id = await self.operator.escalate(
                platform="vk", user_id=user.user_id, chat_id=user.peer_id, username=user.username,
                full_name=user.full_name, question=(original_question or "Запрос на бронирование"), answer=answer,
            )
            text = (
                "Сейчас не получается надёжно проверить календарь бронирований. Я передал запрос специалисту 💛\n\n"
                "Повторно писать не нужно — ответ придёт сюда."
                if ticket_id else
                "Сейчас не получается проверить календарь бронирований. Пожалуйста, воспользуйтесь обратным звонком — сотрудник поможет оформить заявку."
            )
            await self.send_message(user.peer_id, text, keyboard=main_keyboard() if ticket_id else fallback_keyboard())
            return True
        keyboard = parent_booking_confirmation_keyboard() if result.needs_parent_confirmation else main_keyboard()
        if result.text:
            await self.send_message(user.peer_id, result.text, keyboard=keyboard)
        if result.manager_text and result.booking_id and self.settings.vk_manager_user_id:
            try:
                await self.send_message(
                    int(self.settings.vk_manager_user_id),
                    result.manager_text,
                    keyboard=manager_booking_keyboard(result.booking_id),
                )
            except Exception as exc:
                print(f"[booking] manager notification failed: {exc}", flush=True)
        return True

    async def _try_booking_text(self, user: VKUser, text: str) -> bool:
        if self.booking is None or not self.booking.candidate(text, user_id=user.user_id):
            return False
        # Do not leave two competing local forms alive. If this message is handled by Booking,
        # an unfinished Smart Handoff would otherwise wake up later and consume a booking reply.
        if self.intake is not None and self.intake.has_active(user.user_id):
            self.intake.abandon_for_other_flow(user.user_id)
        result = await self.booking.handle_text(
            BookingUser(user.user_id, user.peer_id, user.full_name, user.username), text
        )
        return await self._send_booking_result(user, result, original_question=text)

    @staticmethod
    def _intake_manager_summary(submission: dict[str, Any]) -> str:
        lines: list[str] = []
        for item in submission.get("display_fields") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            if label and value:
                lines.append(f"• {label}: {value}")
        original = str(submission.get("original_question") or "").strip()
        if original:
            lines.append(f"\n💬 Исходное сообщение:\n{original}")
        return "\n".join(lines) or "💬 Данные собраны ботом."

    async def _submit_intake(self, user: VKUser, submission: dict[str, Any], *, claimed: bool) -> bool:
        title = str(submission.get("title") or "Обращение")
        intake_type = str(submission.get("intake_type") or "general")
        priority = str(submission.get("priority") or "normal")
        original = str(submission.get("original_question") or title)
        ticket_id = await self.operator.escalate_intake(
            user_id=user.user_id, chat_id=user.peer_id, username=user.username, full_name=user.full_name,
            question=original, intake_type=intake_type, intake_title=title, intake_payload=submission,
            priority=priority, summary_text=self._intake_manager_summary(submission),
        )
        if claimed and self.intake is not None:
            self.intake.submission_finished(user.user_id, success=bool(ticket_id))
        if ticket_id:
            self.db.track(
                user_id=user.user_id, peer_id=user.peer_id, kind="smart_handoff_submit", name=intake_type,
                meta={"ticket_id": ticket_id, "priority": priority},
            )
            customer_text = (
                "🔴 Сообщение срочно передано специалисту. Повторно отправлять его не нужно."
                if priority == "urgent" else
                "✅ Готово. Я собрал данные и передал заявку специалисту. Повторно писать то же самое не нужно — ответ придёт сюда 💛"
            )
            await self.send_message(user.peer_id, customer_text, keyboard=main_keyboard())
            return True
        if priority == "urgent":
            fail_text = (
                "⚠️ Сейчас не получилось автоматически передать срочное сообщение специалисту. "
                "Если есть непосредственная угроза жизни или здоровью — звоните 112. "
                "Для связи с лагерем воспользуйтесь обратным звонком."
            )
        elif claimed:
            fail_text = (
                "Сейчас не получилось передать заявку специалисту. Собранная заявка сохранена — "
                "попробуйте подтвердить её ещё раз чуть позже или воспользуйтесь обратным звонком."
            )
        else:
            fail_text = (
                "Сейчас не получилось автоматически передать обращение специалисту. "
                "Пожалуйста, воспользуйтесь обратным звонком — так обращение точно не потеряется."
            )
        await self.send_message(user.peer_id, fail_text, keyboard=fallback_keyboard())
        return True

    async def _send_intake_result(self, user: VKUser, result: IntakeResult) -> bool:
        if not result.handled:
            return False
        if result.state in {"submit", "submit_immediate"} and result.submission:
            if result.text and result.state == "submit_immediate":
                await self.send_message(user.peer_id, result.text, keyboard=main_keyboard())
            return await self._submit_intake(user, result.submission, claimed=(result.state == "submit"))
        keyboard = parent_intake_confirmation_keyboard() if result.needs_parent_confirmation else main_keyboard()
        if result.text:
            await self.send_message(user.peer_id, result.text, keyboard=keyboard)
        return True

    async def _try_intake_text(self, user: VKUser, text: str) -> bool:
        if self.intake is None or not self.intake.candidate(text, user_id=user.user_id):
            return False
        # A collecting Booking form can steal simple answers such as «2 смена». Release it before
        # starting Smart Handoff. A booking already pending manager approval is safe to keep.
        if self.booking is not None and self.booking.blocks_other_flow(user.user_id):
            await self.booking.abandon_for_other_flow(user.user_id, reason="switched_to_smart_handoff")
        result = await self.intake.handle_text(
            IntakeUser(user.user_id, user.peer_id, user.full_name, user.username), text
        )
        if result.handled:
            row = self.db.get_intake_session(user_id=user.user_id)
            name = (row or {}).get("intake_type") or (result.submission or {}).get("intake_type") or result.state or "intake"
            self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="smart_handoff", name=str(name))
        return await self._send_intake_result(user, result)

    async def _offer_knowledge_candidate(self, manager: VKUser, ticket, answer_text: str) -> None:
        if not self.settings.knowledge_loop_enabled:
            return
        allowed, _reason = knowledge_candidate_allowed(ticket, answer_text)
        if not allowed:
            return
        ticket_id = int(ticket["id"])
        if str(ticket["knowledge_candidate_status"] or ""):
            return
        try:
            await self.send_message(
                manager.peer_id,
                "📚 Этот ответ похож на устойчивое правило и может пригодиться другим родителям.\n\n"
                "Добавить его в базу знаний? Персональные и динамические обращения сюда автоматически не предлагаются.",
                keyboard=knowledge_candidate_keyboard(ticket_id),
            )
            self.db.set_ticket_knowledge_candidate(ticket_id, "offered")
        except Exception as exc:
            print(f"[knowledge-loop] offer failed for ticket #{ticket_id}: {type(exc).__name__}: {exc}", flush=True)

    # ---------------- manager/admin ----------------
    def _is_manager(self, user_id: int) -> bool:
        return bool(self.settings.vk_manager_user_id and user_id == self.settings.vk_manager_user_id)

    def _stats_text(self, days: int | None = 30) -> str:
        s = self.db.analytics_stats(days=days)
        period = "за 30 дней" if days else "за всё время"
        top = "\n".join(f"• {name}: {count}" for name, count in s["top_buttons"]) or "• пока нет данных"
        return (
            f"📊 СТАТИСТИКА {period.upper()}\n\n"
            f"👥 Уникальных пользователей: {s['users']}\n"
            f"💬 Сообщений: {s['messages']}\n"
            f"🔘 Нажатий на кнопки: {s['buttons']}\n\n"
            f"🤖 Автоответов: {s['auto_answers']}\n"
            f"👨‍💼 Передано специалисту: {s['escalated']}\n"
            f"🆘 Открытых сейчас: {s['open']}\n"
            f"✅ Автоматизация: {s['containment_rate']}%\n\n"
            f"👍 Помогло: {s['helpful']}\n"
            f"👎 Не помогло: {s['not_helpful']}\n"
            f"Полезность оценённых: {s['helpful_rate']}%\n\n"
            f"🔔 Подписаны на уведомления: {s['subscribers']}\n"
            f"📚 Активных знаний менеджера: {self.db.manual_knowledge_count()}\n\n"
            f"ТОП КНОПОК:\n{top}"
        )

    def _open_text(self) -> str:
        rows = self.db.list_open_tickets(10)
        if not rows:
            return "✅ Открытых вопросов сейчас нет."
        parts = ["🆘 ОТКРЫТЫЕ ВОПРОСЫ"]
        for row in rows:
            q = str(row["question"]).replace("\n", " ")
            if len(q) > 130:
                q = q[:127] + "…"
            priority = str(row["priority"] or "normal") if "priority" in row.keys() else "normal"
            intake_type = str(row["intake_type"] or "") if "intake_type" in row.keys() else ""
            icon = {"urgent": "🔴", "high": "🟠", "low": "🟢"}.get(priority, "🟡")
            tag = f" · {intake_type}" if intake_type else ""
            parts.append(f"\n{icon} #{row['id']} · {row['full_name']}{tag}\n{q}")
        parts.append("\nОтвет: #номер текст")
        return "\n".join(parts)

    def _bad_text(self) -> str:
        s = self.db.analytics_stats(days=30)
        rows = s["bad_faq"]
        if not rows:
            return "👍 За последние 30 дней пока нет ответов с оценкой «не помогло»."
        return "👎 ПРОБЛЕМНЫЕ ОТВЕТЫ ЗА 30 ДНЕЙ\n\n" + "\n".join(f"• {faq}: {count}" for faq, count in rows)

    async def _admin_diagnostics_text(self) -> str:
        lines = [f"🧪 Диагностика v{VERSION}", f"VK group: {self.settings.vk_group_id}", f"Manager: {self.settings.vk_manager_user_id or 'не задан'}"]
        lines.append(f"Website KB: {'fresh' if getattr(self.kb, 'is_fresh', False) else 'stale/fallback'}")
        lines.append(f"Manager KB: {self.db.manual_knowledge_count()} active")
        lines.append(f"Smart Handoff: {'ON' if self.settings.smart_handoff_enabled else 'OFF'} · active sessions: {self.db.intake_session_count()}")
        lines.append(f"Event requests: {'ON' if self.intake is not None else 'OFF'}")
        lines.append(f"Knowledge Loop: {'ON' if self.settings.knowledge_loop_enabled else 'OFF'}")
        lines.append(f"VK profile source: {'OK' if self._profile_pages else 'not loaded'}")
        if self.settings.vk_content_token:
            lines.append(f"Recent VK posts: {len(self._wall_pages)} loaded")
        else:
            lines.append("Recent VK posts: DISABLED (VK_CONTENT_TOKEN optional)")
        return "\n".join(lines)

    def _knowledge_list_text(self) -> str:
        rows = self.db.list_manual_knowledge(20, include_expired=True)
        if not rows:
            return "📚 БАЗА ЗНАНИЙ\n\nПока нет знаний, добавленных менеджером."
        now = datetime.now(timezone.utc)
        parts = ["📚 БАЗА ЗНАНИЙ · последние 20"]
        for row in rows:
            active = bool(row.get("active", True))
            expires_raw = str(row.get("expires_at") or "")
            expired = False
            if expires_raw:
                try:
                    dt = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
                    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                    expired = dt.astimezone(timezone.utc) <= now
                except ValueError:
                    expired = True
            if not active:
                status = "🗑 удалено"
            elif expired:
                status = "⌛ истекло"
            elif row.get("kind") == "temporary":
                status = "⏱ временное"
            else:
                status = "✅ постоянное"
            q = str(row.get("question") or "").replace("\n", " ")
            if len(q) > 90:
                q = q[:87] + "…"
            parts.append(f"\n#{row['manual_id']} · {status}\n{q}")
        parts.append("\nУдалить: #удалитьзнание ID")
        return "\n".join(parts)

    @staticmethod
    def _knowledge_preview_text(payload: dict[str, Any]) -> str:
        kind = str(payload.get("kind") or "permanent")
        aliases = payload.get("aliases") or []
        ttl = int(payload.get("ttl_days") or 0)
        kind_line = "⏱ Временное" + (f" · {ttl} дн." if ttl else "") if kind == "temporary" else "✅ Постоянное"
        alias_text = " | ".join(str(x) for x in aliases) if aliases else "—"
        return (
            "📚 ПРЕДПРОСМОТР ЗНАНИЯ\n\n"
            f"Тип: {kind_line}\n\n"
            f"Вопрос:\n{payload.get('question','')}\n\n"
            f"Ответ:\n{payload.get('answer','')}\n\n"
            f"Синонимы:\n{alias_text}\n\n"
            "Сохранить?"
        )

    async def _start_knowledge(self, user: VKUser, kind: str) -> None:
        kind = "temporary" if kind == "temporary" else "permanent"
        self.db.set_admin_state(user_id=user.user_id, state="kb_question", payload={"kind": kind})
        note = (
            "⏱ Временное знание подходит для цены, дат, графика и сезонной информации."
            if kind == "temporary" else
            "✅ Постоянное знание подходит для устойчивого правила или факта."
        )
        await self.send_message(
            user.peer_id,
            f"📚 {note}\n\nШаг 1/3. Отправьте пример вопроса родителя одним сообщением.\n\nДля отмены: #меню",
        )

    async def _start_newsletter(self, user: VKUser) -> None:
        if not self.settings.newsletters_enabled:
            await self.send_message(user.peer_id, "Рассылки отключены в настройках.", keyboard=admin_keyboard())
            return
        self.db.set_admin_state(user_id=user.user_id, state="newsletter_text")
        await self.send_message(user.peer_id, "📢 Отправьте следующим сообщением текст важного объявления.\n\nПосле этого я покажу предпросмотр и количество получателей. Отправка начнётся только после подтверждения.")

    async def _manager_command(self, user: VKUser, text: str) -> bool:
        if not self._is_manager(user.user_id):
            return False
        clean = text.strip()

        if clean.lower() in {"#смены", "#путевки", "#путёвки"} and self.shifts is not None:
            rows = self.shifts.pending_apps()
            if not rows:
                await self.send_message(user.peer_id,"🌟 Необработанных заявок на смены нет.",keyboard=admin_keyboard())
            else:
                for app in rows:
                    await self.send_message(user.peer_id,self.shifts.manager_message(app),
                                            keyboard=manager_shift_keyboard(app['application_id']))
            return True

        # Explicit ticket shortcut.
        match = re.match(r"^\s*#(\d+)\s+([\s\S]+?)\s*$", clean)
        if match:
            ticket_id, answer_text = int(match.group(1)), match.group(2).strip()
            ticket = self.db.get_ticket(ticket_id)
            if not ticket:
                await self.send_message(user.peer_id, f"⚠️ Заявка #{ticket_id} не найдена.", keyboard=admin_keyboard())
                return True
            if str(ticket["status"] or "") != "open":
                await self.send_message(user.peer_id, f"ℹ️ Заявка #{ticket_id} уже обработана. Статус: {ticket['status']}.", keyboard=admin_keyboard())
                return True
            delivered, error = await self.operator.deliver_human_answer(ticket, answer_text)
            await self.send_message(user.peer_id, f"✅ Ответ по заявке #{ticket_id} отправлен пользователю." if delivered else f"⚠️ Не удалось отправить: {error}", keyboard=admin_keyboard())
            if delivered:
                refreshed = self.db.get_ticket(ticket_id) or ticket
                await self._offer_knowledge_candidate(user, refreshed, answer_text)
            return True

        state = self.db.get_admin_state(user_id=user.user_id)
        if state:
            state_name, payload = state
            if state_name == "reply_ticket" and clean and not clean.startswith("#"):
                ticket_id = int(payload.get("ticket_id") or 0)
                ticket = self.db.get_ticket(ticket_id)
                self.db.set_admin_state(user_id=user.user_id, state=None)
                if not ticket or str(ticket["status"] or "") != "open":
                    await self.send_message(user.peer_id, f"⚠️ Заявка #{ticket_id} уже недоступна.", keyboard=admin_keyboard())
                    return True
                delivered, error = await self.operator.deliver_human_answer(ticket, clean)
                await self.send_message(user.peer_id, f"✅ Ответ по заявке #{ticket_id} отправлен." if delivered else f"⚠️ Не удалось отправить: {error}", keyboard=admin_keyboard())
                if delivered:
                    refreshed = self.db.get_ticket(ticket_id) or ticket
                    await self._offer_knowledge_candidate(user, refreshed, clean)
                return True
            if state_name == "knowledge_loop_edit" and clean and not clean.startswith("#"):
                ticket_id = int(payload.get("ticket_id") or 0)
                if not self.settings.knowledge_loop_enabled:
                    self.db.set_admin_state(user_id=user.user_id, state=None)
                    if ticket_id:
                        ticket = self.db.get_ticket(ticket_id)
                        if ticket and str(ticket["knowledge_candidate_status"] or "") == "editing":
                            self.db.set_ticket_knowledge_candidate(ticket_id, "skipped")
                    await self.send_message(user.peer_id, "ℹ️ Knowledge Loop сейчас отключён в настройках.", keyboard=admin_keyboard())
                    return True
                ticket = self.db.get_ticket(ticket_id)
                self.db.set_admin_state(user_id=user.user_id, state=None)
                if not ticket or str(ticket["status"] or "") != "answered":
                    await self.send_message(user.peer_id, f"⚠️ Заявка #{ticket_id} недоступна для базы знаний.", keyboard=admin_keyboard())
                    return True
                if str(ticket["knowledge_candidate_status"] or "") != "editing":
                    await self.send_message(user.peer_id, f"ℹ️ Предложение по заявке #{ticket_id} уже обработано.", keyboard=admin_keyboard())
                    return True
                allowed, reason = knowledge_candidate_allowed(ticket, clean)
                if not allowed:
                    self.db.set_ticket_knowledge_candidate(ticket_id, "rejected")
                    await self.send_message(user.peer_id, f"⚠️ Этот ответ не сохранён как общее знание ({reason}).", keyboard=admin_keyboard())
                    return True
                knowledge_id = self.db.add_manual_knowledge(
                    question=str(ticket["question"]), answer=clean, created_by=user.user_id, kind="permanent"
                )
                if knowledge_id:
                    self.db.set_ticket_knowledge_candidate(ticket_id, "saved", knowledge_id)
                    await self.send_message(user.peer_id, f"✅ Знание #{knowledge_id} сохранено. Следующим родителям бот сможет использовать этот ответ.", keyboard=admin_keyboard())
                else:
                    self.db.set_ticket_knowledge_candidate(ticket_id, "duplicate")
                    await self.send_message(user.peer_id, "ℹ️ Такой вопрос и ответ уже есть в базе знаний.", keyboard=admin_keyboard())
                return True
            if state_name == "kb_question" and clean and not clean.startswith("#"):
                kind = str(payload.get("kind") or "permanent")
                allowed, reason = manual_knowledge_allowed(clean, kind=kind)
                if not allowed:
                    messages = {
                        "dynamic_availability": "⚠️ Текущее наличие мест/свободных дат нельзя сохранять в базу: оно меняется слишком быстро. Такой вопрос всегда передаётся специалисту.",
                        "volatile_requires_temporary": "⏱ Этот факт может меняться. Добавьте его как временное знание.",
                        "sensitive_or_individual": "🔐 Индивидуальные или персональные данные нельзя сохранять как знание бота.",
                        "medicine_permission_requires_human": "🩺 Индивидуальное решение по лекарствам должен давать сотрудник.",
                    }
                    await self.send_message(user.peer_id, messages.get(reason, "⚠️ Это знание нельзя сохранить автоматически."), keyboard=knowledge_menu_keyboard())
                    self.db.set_admin_state(user_id=user.user_id, state=None)
                    return True
                payload["question"] = clean
                self.db.set_admin_state(user_id=user.user_id, state="kb_answer", payload=payload)
                await self.send_message(user.peer_id, "Шаг 2/3. Теперь отправьте утверждённый правильный ответ родителю.")
                return True
            if state_name == "kb_answer" and clean and not clean.startswith("#"):
                payload["answer"] = clean
                self.db.set_admin_state(user_id=user.user_id, state="kb_aliases", payload=payload)
                await self.send_message(user.peer_id, "Шаг 3/3. Отправьте варианты вопроса через |\n\nНапример: Что взять для договора? | Документы при покупке путёвки\n\nЕсли синонимы не нужны — отправьте «-».")
                return True
            if state_name == "kb_aliases" and clean and not clean.startswith("#"):
                aliases = [] if clean.lower() in {"-", "—", "нет", "пропустить"} else [x.strip() for x in clean.split("|") if x.strip()]
                payload["aliases"] = aliases[:20]
                if str(payload.get("kind")) == "temporary":
                    self.db.set_admin_state(user_id=user.user_id, state="kb_ttl", payload=payload)
                    await self.send_message(user.peer_id, "⏱ На какой срок знание должно действовать?", keyboard=knowledge_ttl_keyboard())
                else:
                    self.db.set_admin_state(user_id=user.user_id, state="kb_preview", payload=payload)
                    await self.send_message(user.peer_id, self._knowledge_preview_text(payload), keyboard=knowledge_preview_keyboard())
                return True
            if state_name == "kb_ttl" and clean and not clean.startswith("#"):
                m = re.search(r"\b(1|7|30|90)\b", clean)
                if not m:
                    await self.send_message(user.peer_id, "Выберите срок кнопкой: 1, 7, 30 или 90 дней.", keyboard=knowledge_ttl_keyboard())
                    return True
                payload["ttl_days"] = int(m.group(1))
                self.db.set_admin_state(user_id=user.user_id, state="kb_preview", payload=payload)
                await self.send_message(user.peer_id, self._knowledge_preview_text(payload), keyboard=knowledge_preview_keyboard())
                return True
            if state_name == "kb_preview" and clean and not clean.startswith("#"):
                await self.send_message(user.peer_id, "Используйте кнопки «Сохранить» или «Отмена» под предпросмотром.", keyboard=knowledge_preview_keyboard())
                return True
            if state_name == "newsletter_text" and clean and not clean.startswith("#"):
                peers = self.db.subscriber_peer_ids()
                campaign_id = self.db.create_campaign(text=clean, created_by=user.user_id, target_count=len(peers))
                self.db.set_admin_state(user_id=user.user_id, state=None)
                preview = f"📢 ПРЕДПРОСМОТР РАССЫЛКИ #{campaign_id}\n\n{clean}\n\n👥 Получателей: {len(peers)}\n\nОтправить?"
                await self.send_message(user.peer_id, preview, keyboard=newsletter_preview_keyboard(campaign_id))
                return True

        cmd = clean.lower()
        if cmd in {"#меню", "#menu", "#admin"}:
            if state and state[0] == "knowledge_loop_edit":
                try:
                    ticket_id = int((state[1] or {}).get("ticket_id") or 0)
                except (TypeError, ValueError):
                    ticket_id = 0
                if ticket_id:
                    ticket = self.db.get_ticket(ticket_id)
                    if ticket and str(ticket["knowledge_candidate_status"] or "") == "editing":
                        self.db.set_ticket_knowledge_candidate(ticket_id, "skipped")
            self.db.set_admin_state(user_id=user.user_id, state=None)
            await self.send_message(user.peer_id, "🛠 ПАНЕЛЬ АДМИНИСТРАТОРА", keyboard=admin_keyboard())
            return True
        if cmd in {"#stats", "#статистика"}:
            await self.send_message(user.peer_id, self._stats_text(), keyboard=admin_keyboard())
            return True
        if cmd in {"#open", "#вопросы"}:
            await self.send_message(user.peer_id, self._open_text(), keyboard=admin_keyboard())
            return True
        if cmd in {"#рассылка", "#newsletter"}:
            await self._start_newsletter(user)
            return True
        if cmd in {"#знания", "#knowledge", "#kb"}:
            self.db.set_admin_state(user_id=user.user_id, state=None)
            await self.send_message(user.peer_id, self._knowledge_list_text(), keyboard=knowledge_menu_keyboard())
            return True
        delete_match = re.match(r"^#удалитьзнание\s+(\d+)\s*$", cmd)
        if delete_match:
            knowledge_id = int(delete_match.group(1))
            ok = self.db.delete_manual_knowledge(knowledge_id)
            await self.send_message(user.peer_id, f"🗑 Знание #{knowledge_id} отключено." if ok else f"⚠️ Активное знание #{knowledge_id} не найдено.", keyboard=knowledge_menu_keyboard())
            return True
        return False

    async def _send_newsletter(self, manager: VKUser, campaign_id: int) -> None:
        campaign = self.db.get_campaign(campaign_id)
        if not campaign or str(campaign["status"]) != "draft":
            await self.send_message(manager.peer_id, "⚠️ Черновик рассылки не найден или уже отправлен.", keyboard=admin_keyboard())
            return
        peers = self.db.subscriber_peer_ids()
        if not peers:
            self.db.finish_campaign(campaign_id, sent=0, failed=0)
            await self.send_message(manager.peer_id, "ℹ️ Сейчас нет подписчиков на важные уведомления.", keyboard=admin_keyboard())
            return
        text = str(campaign["text"])
        sent = failed = 0
        for i in range(0, len(peers), 100):
            batch = peers[i:i + 100]
            params = {
                "peer_ids": batch,
                "random_id": random.randint(1, 2_000_000_000),
                "message": text + "\n\n🔕 Отключить уведомления можно через кнопку «Важные уведомления» в меню.",
                "intent": "non_promo_newsletter",
            }
            try:
                # Do not bypass VK newsletter semantics by silently retrying as a regular message.
                # If VK rejects non_promo_newsletter, fail that batch and report it to the manager.
                response = await self.api("messages.send", **params)
                if isinstance(response, list):
                    for item in response:
                        if isinstance(item, dict) and item.get("error"):
                            failed += 1
                        else:
                            sent += 1
                else:
                    sent += len(batch)
            except Exception as exc:
                print(f"[newsletter] batch failed: {exc}", flush=True)
                failed += len(batch)
            await asyncio.sleep(0.35)
        self.db.finish_campaign(campaign_id, sent=sent, failed=failed)
        self.db.track(user_id=manager.user_id, peer_id=manager.peer_id, kind="newsletter", name="sent", meta={"campaign_id": campaign_id, "sent": sent, "failed": failed})
        await self.send_message(manager.peer_id, f"✅ Рассылка #{campaign_id} завершена.\n\n📨 Отправлено: {sent}\n⚠️ Не доставлено: {failed}", keyboard=admin_keyboard())

    # ---------------- updates ----------------
    async def _handle_message_new(self, update: dict[str, Any]) -> None:
        obj = update.get("object") or {}
        message = obj.get("message") if isinstance(obj, dict) else None
        if not isinstance(message, dict):
            message = obj if isinstance(obj, dict) else {}
        from_id = int(message.get("from_id") or 0)
        peer_id = int(message.get("peer_id") or from_id or 0)
        if from_id <= 0 or peer_id <= 0:
            return
        text = str(message.get("text") or "").strip()
        attachments = message.get("attachments") or []
        payload = self._decode_payload(message.get("payload"))
        intent = str(payload.get("intent") or "").strip() or None
        user = await self._user(from_id, peer_id)

        first_contact = not self.db.has_seen_user(user_id=user.user_id, platform="vk")
        self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="message", name=intent or "text")
        if intent:
            self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="button", name=intent)

        if await self._manager_command(user, text):
            return

        if intent == "start" or text.lower() in {"начать", "/start", "start"}:
            await self._welcome(user)
            return

        # A first-time parent may skip the Start button and immediately ask a real question.
        # Send the intro once, but NEVER consume/ignore that question: the same incoming
        # message continues through booking/FAQ/human handoff below.
        if first_contact:
            await self.send_message(user.peer_id, html_to_vk_text(WELCOME), keyboard=main_keyboard())

        # A plain greeting is social contact, not an unanswered support request. Never escalate it.
        if not intent and text and is_greeting_only(text):
            greeting, _ = split_leading_greeting(text)
            self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="smalltalk", name="greeting")
            if not first_contact:
                await self.send_message(peer_id, greeting_intro(greeting), keyboard=main_keyboard())
            return

        # VK stickers/attachments often arrive with an empty text field. Respond like a person instead of
        # telling the parent that the bot "cannot process" the message.
        if not text and not intent:
            sticker = isinstance(attachments, list) and any(
                isinstance(item, dict) and (str(item.get("type") or "") == "sticker" or bool(item.get("sticker")))
                for item in attachments
            )
            self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="smalltalk", name="sticker" if sticker else "attachment")
            if not first_contact:
                await self.send_message(peer_id, attachment_intro(sticker=sticker), keyboard=main_keyboard())
            return

        if len(text) > 3500:
            await self.send_message(peer_id, "Сообщение получилось очень длинным. Сформулируйте, пожалуйста, один вопрос покороче 🙂", keyboard=main_keyboard())
            return

        # If the parent greets us and asks a question in the same message, keep the greeting in the reply
        # while routing only the meaningful question text.
        greeting = None
        question_text = text
        if not intent and text:
            greeting, rest = split_leading_greeting(text)
            if greeting and rest:
                question_text = rest
        if first_contact:
            greeting = None

        # Safety takes precedence over every optional feature. An urgent message must never be
        # swallowed by an unfinished booking merely because Smart Handoff is disabled.
        if not intent and is_urgent_safety(question_text):
            if self.shifts is not None and self.shifts.has_active(user.user_id):
                self.shifts.abandon(user.user_id)
            # An emergency also terminates any unfinished booking. Otherwise the old booking
            # state (and possibly an external hold) could wake up on the parent's next short
            # message after the urgent handoff. A booking already submitted to the manager is
            # intentionally left intact.
            if self.booking is not None and self.booking.blocks_other_flow(user.user_id):
                await self.booking.abandon_for_other_flow(user.user_id, reason="urgent_safety")
            # Clear even a stale persisted intake left from an earlier run where the feature was
            # enabled. This prevents an old questionnaire from resurfacing if the flag is turned
            # back on after an emergency.
            if self.intake is None:
                self.db.clear_intake_session(user_id=user.user_id)
            if self.intake is not None:
                result = await self.intake.handle_text(
                    IntakeUser(user.user_id, user.peer_id, user.full_name, user.username), question_text
                )
            else:
                result = IntakeResult(
                    True,
                    "🔴 Я сразу передам это сообщение специалисту как срочное. Если есть непосредственная угроза жизни или здоровью — одновременно звоните 112.",
                    state="submit_immediate",
                    submission=build_urgent_submission(question_text),
                )
            await self._send_intake_result(user, result)
            return

        # Never persist/forward card numbers, CVC/CVV or SMS codes even when Smart Handoff is
        # disabled. Message analytics above stores metadata only, not the parent text.
        if not intent and contains_payment_secret(question_text):
            await self.send_message(
                user.peer_id,
                "🔐 Не присылайте номер банковской карты, CVC/CVV или коды из SMS. Удалите эти данные и опишите только саму проблему с оплатой.",
                keyboard=main_keyboard(),
            )
            return

        # Dedicated shift-application form precedes generic Smart Handoff and static FAQ.
        if not intent and await self._try_shift_text(user, question_text):
            return

        # Etiquette / acknowledgement messages are complete conversations, not unanswered
        # support questions.  Handle them before Booking, Smart Handoff and the generic
        # fallback so "спасибо" or "до свидания" never creates a manager ticket.
        if not intent:
            social_kind = classify_social_only(question_text)
            if social_kind:
                self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="smalltalk", name=social_kind)
                await self.send_message(user.peer_id, social_reply(social_kind), keyboard=main_keyboard())
                try:
                    await self.mark_support_conversation(user.peer_id, False)
                except Exception:
                    pass
                return

        if not intent:
            event_info = public_event_info(question_text)
            if event_info:
                self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="faq", name="event_public_info")
                await self.send_message(user.peer_id, event_info, keyboard=main_keyboard())
                return

        # Stable public rental prices/conditions come from the official camp offer for this
        # release. Answer them before Booking so a simple price question never starts a form
        # or creates a manager ticket. Dynamic availability still goes through Booking.
        if not intent:
            rental_info = public_rental_info(question_text)
            if rental_info:
                self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="faq", name="rental_public_info")
                await self.send_message(user.peer_id, rental_info, keyboard=main_keyboard())
                return

        # If the Google booking backend is intentionally disabled or temporarily unavailable,
        # explicit booking requests must fail closed to a human instead of looking like a static
        # extra-services FAQ answer. Informational price/conditions questions still use the FAQ.
        if not intent and self.booking is None and is_booking_request(question_text):
            if self.intake is not None and classify_transaction(question_text) == "corpus_request":
                intake_result = await self.intake.handle_text(
                    IntakeUser(user.user_id, user.peer_id, user.full_name, user.username), question_text
                )
                await self._send_intake_result(user, intake_result)
                return
            # The parent explicitly switched from an unfinished Smart Handoff to booking. Even
            # though the booking backend is disabled/misconfigured, do not leave the old form
            # alive to consume their next reply.
            if self.intake is not None and self.intake.has_active(user.user_id):
                self.intake.abandon_for_other_flow(user.user_id)
            await self._send_booking_result(
                user,
                BookingResult(True, state="escalate", error_reason="booking_not_enabled"),
                original_question=question_text,
            )
            return

        if not intent and await self._try_booking_text(user, question_text):
            return
        if not intent and await self._try_intake_text(user, question_text):
            return

        await self._process_question(text=question_text or intent or "", intent=intent, user=user, greeting=greeting)

    async def _handle_message_event(self, update: dict[str, Any]) -> None:
        obj = update.get("object") or {}
        if not isinstance(obj, dict):
            return
        payload = self._decode_payload(obj.get("payload"))
        action = str(payload.get("action") or "")
        peer_id = int(obj.get("peer_id") or 0)
        user_id = int(obj.get("user_id") or 0)
        user = await self._user(user_id, peer_id) if user_id and peer_id else None
        if user:
            self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="button", name=f"callback:{action}")

        if action == "shift_manager" and user and self._is_manager(user.user_id) and self.shifts is not None:
            app_id = str(payload.get('application_id') or '')
            cmd = str(payload.get('cmd') or '')
            if cmd not in {'approve','reject'} or not re.fullmatch(r'ZV-[A-F0-9]{12}',app_id):
                await self._event_answer(obj,'Некорректная команда')
                return
            app, updated = await self.shifts.manager_decision(app_id,cmd=='approve',user.user_id)
            if app is None:
                await self._event_answer(obj,'Заявка не найдена')
                return
            if not updated:
                await self._event_answer(obj,'Уже обработана')
                return
            await self._event_answer(obj,'Статус заявки обновлён')
            msg = ('✅ Менеджер рассмотрел и подтвердил Вашу заявку на смену. '
                   'Для завершения оформления путёвки и оплаты с Вами свяжется сотрудник лагеря.'
                   if cmd=='approve' else
                   'К сожалению, заявку на эту смену не удалось подтвердить. Уточнить альтернативы можно у менеджера.')
            try:
                await self.send_message(int(app['peer_id']),msg,keyboard=main_keyboard())
            except Exception as exc:
                print(f'[shift] parent status delivery failed: {type(exc).__name__}',flush=True)
            await self.send_message(user.peer_id,f'Заявка {app_id}: {"подтверждена" if cmd=="approve" else "отменена"}.',keyboard=admin_keyboard())
            return

        if action == "booking_parent" and user and self.booking is not None:
            cmd = str(payload.get("cmd") or "")
            if cmd not in {"confirm", "cancel"}:
                await self._event_answer(obj, "Неизвестное действие")
                return
            result = await self.booking.parent_decision(
                BookingUser(user.user_id, user.peer_id, user.full_name, user.username),
                confirm=(cmd == "confirm"),
            )
            if result.state == "pending_manager":
                event_text = "Заявка отправлена менеджеру"
            elif result.state == "cancelled":
                event_text = "Заявка отменена"
            elif result.state == "stale":
                event_text = "Заявка уже обработана"
            elif result.state == "escalate":
                event_text = "Не удалось проверить календарь"
            else:
                event_text = "Готово"
            await self._event_answer(obj, event_text)
            await self._send_booking_result(user, result, original_question="Подтверждение заявки на бронирование")
            return

        if action == "booking_manager" and user and self._is_manager(user.user_id) and self.booking is not None:
            booking_id = str(payload.get("booking_id") or "")
            cmd = str(payload.get("cmd") or "")
            if cmd not in {"approve", "reject"} or not booking_id:
                await self._event_answer(obj, "Некорректная команда")
                return
            approve = cmd == "approve"
            try:
                result = await self.booking.manager_decision(booking_id=booking_id, manager_id=user.user_id, approve=approve)
            except BookingAPIError as exc:
                await self._event_answer(obj, "Не удалось обновить заявку")
                await self.send_message(user.peer_id, f"⚠️ Заявка {booking_id}: {exc}", keyboard=admin_keyboard())
                return
            await self._event_answer(obj, "Бронь подтверждена" if approve else "Заявка отклонена")
            parent_peer = int(result.get("peer_id") or 0)
            if parent_peer:
                parent_text = (
                    "✅ Менеджер подтвердил Вашу бронь. Заявка окончательно подтверждена 💛"
                    if approve else
                    "К сожалению, менеджер не смог подтвердить эту заявку. Если хотите, напишите другую дату или время — я проверю их заново."
                )
                await self.send_message(parent_peer, parent_text, keyboard=main_keyboard())
            await self.send_message(user.peer_id, f"✅ Заявка {booking_id} обновлена.", keyboard=admin_keyboard())
            return

        if action == "intake_parent" and user:
            if self.intake is None:
                await self._event_answer(obj, "Заявка больше неактуальна")
                await self.send_message(
                    user.peer_id,
                    "Эта кнопка относится к ранее начатой заявке, а Smart Handoff сейчас отключён. "
                    "Напишите вопрос обычным сообщением — я отвечу или передам его специалисту.",
                    keyboard=main_keyboard(),
                )
                return
            cmd = str(payload.get("cmd") or "")
            if cmd not in {"confirm", "cancel"}:
                await self._event_answer(obj, "Неизвестное действие")
                return
            result = await self.intake.parent_decision(
                IntakeUser(user.user_id, user.peer_id, user.full_name, user.username),
                confirm=(cmd == "confirm"),
            )
            event_text = {
                "submit": "Передаю специалисту",
                "cancelled": "Заявка отменена",
                "stale": "Заявка уже обработана",
            }.get(result.state, "Готово")
            await self._event_answer(obj, event_text)
            await self._send_intake_result(user, result)
            return

        if action == "feedback":
            try: feedback_id = int(payload.get("id") or 0)
            except (TypeError, ValueError): feedback_id = 0
            event = self.db.get_answer_event(feedback_id) if feedback_id else None
            if not event or str(event["platform"] or "") != "vk" or int(event["chat_id"]) != peer_id:
                await self._event_answer(obj, "Не удалось обработать оценку")
                return
            helpful = payload.get("value") == "yes"
            self.db.set_feedback(feedback_id, helpful)
            if helpful:
                await self._event_answer(obj, "Спасибо! 💛")
                return
            await self._event_answer(obj, "Передаю вопрос специалисту…")
            if event["escalated_ticket_id"]:
                await self.send_message(peer_id, "💛 Ваш вопрос уже передан специалисту. Ответ придёт сюда.")
                return
            assert user is not None
            answer = BotAnswer(False, None, confidence=float(event["confidence"] or 0.0), reason="negative_feedback", faq_id=event["faq_id"])
            ticket_id = await self.operator.escalate(
                platform="vk", user_id=user.user_id, chat_id=user.peer_id, username=user.username,
                full_name=user.full_name, question=str(event["question"]), answer=answer,
            )
            if ticket_id:
                self.db.link_feedback_ticket(feedback_id, ticket_id)
                await self.send_message(peer_id, html_to_vk_text(ESCALATED), keyboard=main_keyboard())
            else:
                await self.send_message(peer_id, html_to_vk_text(ESCALATED_NO_CHANNEL), keyboard=fallback_keyboard())
            return

        if action == "notifications" and user:
            enabled = payload.get("value") == "on"
            self.db.set_subscription(user_id=user.user_id, peer_id=user.peer_id, enabled=enabled)
            await self._event_answer(obj, "Уведомления включены 🔔" if enabled else "Уведомления отключены")
            await self.send_message(user.peer_id, "🔔 Вы будете получать только важные объявления лагеря." if enabled else "🔕 Важные уведомления отключены.", keyboard=notification_keyboard(enabled))
            return

        if action == "clarify" and user:
            if payload.get("intent") == "camp_address":
                await self._event_answer(obj, "Передаю специалисту")
                answer = BotAnswer(False, None, confidence=1.0, reason="camp_address_not_confirmed")
                ticket_id = await self.operator.escalate(
                    platform="vk", user_id=user.user_id, chat_id=user.peer_id, username=user.username,
                    full_name=user.full_name, question="Какой точный адрес лагеря?", answer=answer,
                )
                await self.send_message(user.peer_id, html_to_vk_text(ESCALATED if ticket_id else ESCALATED_NO_CHANNEL), keyboard=main_keyboard() if ticket_id else fallback_keyboard())
            return

        if action == "manager_reply" and user and self._is_manager(user.user_id):
            ticket_id = int(payload.get("ticket_id") or 0)
            ticket = self.db.get_ticket(ticket_id)
            if not ticket or str(ticket["status"] or "") != "open":
                await self._event_answer(obj, "Заявка уже обработана")
                return
            self.db.set_admin_state(user_id=user.user_id, state="reply_ticket", payload={"ticket_id": ticket_id})
            await self._event_answer(obj, f"Ответ для #{ticket_id}")
            await self.send_message(user.peer_id, f"✍ Напишите следующим сообщением ответ для заявки #{ticket_id}.\n\nДля отмены отправьте #меню.")
            return

        if action == "knowledge_loop" and user and self._is_manager(user.user_id):
            if not self.settings.knowledge_loop_enabled:
                await self._event_answer(obj, "Knowledge Loop отключён")
                return
            cmd = str(payload.get("cmd") or "")
            try:
                ticket_id = int(payload.get("ticket_id") or 0)
            except (TypeError, ValueError):
                ticket_id = 0
            ticket = self.db.get_ticket(ticket_id) if ticket_id else None
            if not ticket or str(ticket["status"] or "") != "answered":
                await self._event_answer(obj, "Ответ уже недоступен")
                return
            if str(ticket["knowledge_candidate_status"] or "") != "offered":
                await self._event_answer(obj, "Уже обработано")
                return
            answer_text = str(ticket["human_answer"] or "").strip()
            allowed, reason = knowledge_candidate_allowed(ticket, answer_text)
            if not allowed:
                self.db.set_ticket_knowledge_candidate(ticket_id, "rejected")
                await self._event_answer(obj, "Нельзя сохранить")
                await self.send_message(user.peer_id, f"⚠️ Этот ответ нельзя превратить в постоянное знание ({reason}).", keyboard=admin_keyboard())
                return
            if cmd == "skip":
                self.db.set_ticket_knowledge_candidate(ticket_id, "skipped")
                await self._event_answer(obj, "Не добавляем")
                return
            if cmd == "edit":
                self.db.set_ticket_knowledge_candidate(ticket_id, "editing")
                self.db.set_admin_state(user_id=user.user_id, state="knowledge_loop_edit", payload={"ticket_id": ticket_id})
                await self._event_answer(obj, "Пришлите исправленный ответ")
                await self.send_message(
                    user.peer_id,
                    f"✏️ Отправьте следующим сообщением общий ответ для базы знаний по вопросу:\n\n{ticket['question']}\n\n"
                    "Не включайте персональные данные или временную доступность. Для отмены: #меню",
                )
                return
            if cmd == "add":
                knowledge_id = self.db.add_manual_knowledge(
                    question=str(ticket["question"]), answer=answer_text, created_by=user.user_id, kind="permanent"
                )
                if knowledge_id:
                    self.db.set_ticket_knowledge_candidate(ticket_id, "saved", knowledge_id)
                    await self._event_answer(obj, "Добавлено в базу")
                    await self.send_message(user.peer_id, f"✅ Знание #{knowledge_id} добавлено. Перезапуск бота не нужен.", keyboard=admin_keyboard())
                else:
                    self.db.set_ticket_knowledge_candidate(ticket_id, "duplicate")
                    await self._event_answer(obj, "Уже есть в базе")
                return
            await self._event_answer(obj, "Неизвестное действие")
            return

        if action == "admin" and user and self._is_manager(user.user_id):
            cmd = str(payload.get("cmd") or "")
            await self._event_answer(obj, "Готово")
            if cmd == "stats": await self.send_message(user.peer_id, self._stats_text(), keyboard=admin_keyboard())
            elif cmd == "open": await self.send_message(user.peer_id, self._open_text(), keyboard=admin_keyboard())
            elif cmd == "bad": await self.send_message(user.peer_id, self._bad_text(), keyboard=admin_keyboard())
            elif cmd == "diag": await self.send_message(user.peer_id, await self._admin_diagnostics_text(), keyboard=admin_keyboard())
            elif cmd == "newsletter": await self._start_newsletter(user)
            elif cmd == "knowledge": await self.send_message(user.peer_id, self._knowledge_list_text(), keyboard=knowledge_menu_keyboard())
            return

        if action == "knowledge" and user and self._is_manager(user.user_id):
            cmd = str(payload.get("cmd") or "")
            if cmd in {"permanent", "temporary"}:
                await self._event_answer(obj, "Добавляем знание")
                await self._start_knowledge(user, cmd)
                return
            if cmd == "list":
                await self._event_answer(obj, "База знаний")
                await self.send_message(user.peer_id, self._knowledge_list_text(), keyboard=knowledge_menu_keyboard())
                return
            if cmd == "back":
                self.db.set_admin_state(user_id=user.user_id, state=None)
                await self._event_answer(obj, "Админ-меню")
                await self.send_message(user.peer_id, "🛠 ПАНЕЛЬ АДМИНИСТРАТОРА", keyboard=admin_keyboard())
                return
            if cmd == "cancel":
                self.db.set_admin_state(user_id=user.user_id, state=None)
                await self._event_answer(obj, "Отменено")
                await self.send_message(user.peer_id, "❌ Добавление знания отменено.", keyboard=knowledge_menu_keyboard())
                return
            state = self.db.get_admin_state(user_id=user.user_id)
            state_name, state_payload = state if state else ("", {})
            if cmd == "ttl":
                if state_name != "kb_ttl":
                    await self._event_answer(obj, "Сначала начните добавление знания")
                    return
                days = int(payload.get("days") or 0)
                if days not in {1, 7, 30, 90}:
                    await self._event_answer(obj, "Неверный срок")
                    return
                state_payload["ttl_days"] = days
                self.db.set_admin_state(user_id=user.user_id, state="kb_preview", payload=state_payload)
                await self._event_answer(obj, f"{days} дн.")
                await self.send_message(user.peer_id, self._knowledge_preview_text(state_payload), keyboard=knowledge_preview_keyboard())
                return
            if cmd == "save":
                if state_name != "kb_preview":
                    await self._event_answer(obj, "Предпросмотр не найден")
                    return
                kind = str(state_payload.get("kind") or "permanent")
                question = str(state_payload.get("question") or "").strip()
                answer = str(state_payload.get("answer") or "").strip()
                allowed, reason = manual_knowledge_allowed(question, kind=kind)
                if not allowed or not question or not answer:
                    self.db.set_admin_state(user_id=user.user_id, state=None)
                    await self._event_answer(obj, "Нельзя сохранить")
                    await self.send_message(user.peer_id, f"⚠️ Знание не сохранено ({reason}).", keyboard=knowledge_menu_keyboard())
                    return
                expires_at = None
                if kind == "temporary":
                    days = int(state_payload.get("ttl_days") or 0)
                    if days not in {1, 7, 30, 90}:
                        await self._event_answer(obj, "Выберите срок")
                        await self.send_message(user.peer_id, "⏱ Выберите срок временного знания.", keyboard=knowledge_ttl_keyboard())
                        return
                    expires_at = datetime.now(timezone.utc) + timedelta(days=days)
                knowledge_id = self.db.add_manual_knowledge(
                    question=question, answer=answer, aliases=list(state_payload.get("aliases") or []),
                    kind=kind, expires_at=expires_at, created_by=user.user_id,
                )
                self.db.set_admin_state(user_id=user.user_id, state=None)
                if knowledge_id:
                    await self._event_answer(obj, "Сохранено")
                    await self.send_message(user.peer_id, f"✅ Знание #{knowledge_id} сохранено и уже участвует в ответах — перезапуск не нужен.", keyboard=knowledge_menu_keyboard())
                else:
                    await self._event_answer(obj, "Дубликат")
                    await self.send_message(user.peer_id, "ℹ️ Такое знание уже есть в базе.", keyboard=knowledge_menu_keyboard())
                return
            await self._event_answer(obj, "База знаний")
            return

        if action == "newsletter" and user and self._is_manager(user.user_id):
            cmd = str(payload.get("cmd") or "")
            campaign_id = int(payload.get("campaign_id") or 0)
            if cmd == "send":
                await self._event_answer(obj, "Запускаю рассылку")
                await self._send_newsletter(user, campaign_id)
            elif cmd == "edit":
                self.db.set_admin_state(user_id=user.user_id, state="newsletter_text")
                await self._event_answer(obj, "Пришлите новый текст")
                await self.send_message(user.peer_id, "✏ Отправьте новый текст рассылки следующим сообщением.")
            elif cmd == "cancel":
                self.db.set_admin_state(user_id=user.user_id, state=None)
                await self._event_answer(obj, "Отменено")
                await self.send_message(user.peer_id, "❌ Рассылка отменена.", keyboard=admin_keyboard())
            return

        await self._event_answer(obj, "Готово")

    async def _handle_update(self, update: dict[str, Any]) -> None:
        typ = str(update.get("type") or "")
        peer_id = self._update_peer_id(update)
        typing_task: asyncio.Task[None] | None = None
        if peer_id > 0 and typ in {"message_new", "message_event"}:
            # Run the indicator independently so a temporary setActivity problem can never
            # delay the actual answer.  The loop sends immediately, then refreshes itself.
            typing_task = asyncio.create_task(self._typing_keepalive(peer_id))
        try:
            if typ == "message_new":
                await self._handle_message_new(update)
            elif typ == "message_event":
                await self._handle_message_event(update)
        finally:
            if typing_task is not None:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass

    # ---------------- secondary knowledge sources ----------------
    def _publish_external_pages(self) -> None:
        if self.kb is not None:
            self.kb.set_external_pages(self._profile_pages + self._wall_pages)

    async def sync_source_profile(self) -> int:
        domain = self.settings.vk_source_domain.strip()
        if not domain or self.kb is None:
            return 0
        response = await self.api(
            "groups.getById",
            group_id=domain,
            fields="description,status,site,contacts,addresses",
        )
        groups = (response.get("groups") or []) if isinstance(response, dict) else (response or [])
        if not groups or not isinstance(groups[0], dict):
            return 0
        g = groups[0]
        self.source_group_id = abs(int(g.get("id") or 0)) or None
        pieces = [
            f"Название: {g.get('name','')}",
            f"Статус: {g.get('status','')}",
            f"Описание: {g.get('description','')}",
            f"Сайт: {g.get('site','')}",
        ]
        for c in g.get("contacts") or []:
            if isinstance(c, dict):
                pieces.append("Контакт: " + " ".join(str(c.get(k) or "") for k in ("desc", "phone", "email") if c.get(k)))
        for a in g.get("addresses") or []:
            if isinstance(a, dict):
                pieces.append("Адрес из профиля VK: " + str(a.get("address") or a.get("title") or ""))
        text = "\n".join(x for x in pieces if x.split(":", 1)[-1].strip())
        self._profile_pages = [{
            "url": f"https://vk.ru/{domain}",
            "title": "Официальный профиль сообщества ВКонтакте",
            "chunks": [text],
            "source_kind": "vk_profile",
        }]
        self._publish_external_pages()
        return 1

    async def sync_recent_wall(self) -> int:
        # wall.get is never called with the community token. It requires the optional content token.
        token = self.settings.vk_content_token
        if not token or self.kb is None:
            self._wall_pages = []
            self._publish_external_pages()
            return 0
        group_id = self.source_group_id
        if not group_id:
            await self.sync_source_profile()
            group_id = self.source_group_id
        if not group_id:
            return 0
        response = await self.api(
            "wall.get",
            token=token,
            owner_id=-group_id,
            count=self.settings.vk_source_post_limit,
            filter="owner",
        )
        items = (response or {}).get("items", []) if isinstance(response, dict) else []
        min_ts = datetime.now(timezone.utc).timestamp() - self.settings.vk_source_max_age_days * 86400
        pages: list[dict[str, Any]] = []
        for post in items:
            if not isinstance(post, dict):
                continue
            post_ts = float(post.get("date") or 0)
            if post_ts and post_ts < min_ts:
                continue
            text = str(post.get("text") or "").strip()
            post_id = post.get("id")
            if not text or not post_id:
                continue
            pages.append({
                "url": f"https://vk.ru/wall-{group_id}_{post_id}",
                "title": "Свежая публикация официального сообщества",
                "chunks": [text],
                "source_kind": "vk_wall_recent",
            })
        self._wall_pages = pages
        self._publish_external_pages()
        return len(pages)

    async def run(self) -> None:
        backoff = 2.0
        last_shift_sync = 0.0
        while not self._stopping:
            try:
                lp = await self.validate()
                try:
                    profile_count = await self.sync_source_profile()
                    print(f"[vk] source profile: {'OK' if profile_count else 'not loaded'}", flush=True)
                except Exception as exc:
                    print(f"[vk] source profile skipped: {type(exc).__name__}: {exc}", flush=True)
                if self.settings.vk_content_token:
                    try:
                        count = await self.sync_recent_wall()
                        print(f"[vk] recent wall evidence loaded: {count} posts", flush=True)
                    except Exception as exc:
                        print(f"[vk] recent wall sync skipped: {type(exc).__name__}: {exc}", flush=True)
                else:
                    print("[vk] recent wall posts: DISABLED (VK_CONTENT_TOKEN optional)", flush=True)

                server, key, ts = str(lp["server"]), str(lp["key"]), str(lp["ts"])
                print(f"[vk] LONG POLL STARTED: group_id={self.settings.vk_group_id}" + (f" name={self.group_name}" if self.group_name else ""), flush=True)
                backoff = 2.0
                while not self._stopping:
                    response = await self.client.get(server, params={"act": "a_check", "key": key, "ts": ts, "wait": 25}, timeout=35.0)
                    response.raise_for_status()
                    payload = response.json()
                    failed = payload.get("failed")
                    if failed:
                        if int(failed) == 1:
                            ts = str(payload.get("ts", ts)); continue
                        break
                    ts = str(payload.get("ts", ts))
                    if self.shifts is not None:
                        from time import monotonic
                        now_mono = monotonic()
                        if now_mono - last_shift_sync >= 60:
                            last_shift_sync = now_mono
                            if self._shift_sync_task is None or self._shift_sync_task.done():
                                self._shift_sync_task = asyncio.create_task(self.shifts.retry_pending(limit=3))
                    for update in payload.get("updates", []) or []:
                        if isinstance(update, dict):
                            try:
                                await self._handle_update(update)
                            except Exception as exc:
                                print(f"[vk] update error: {type(exc).__name__}: {exc}", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[vk] long poll error: {type(exc).__name__}: {exc}; retry in {backoff:.0f}s", flush=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
