from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import Settings
from .core import SupportCore
from .conversation import attachment_intro, greeting_intro, is_greeting_only, split_leading_greeting, with_greeting
from .db import Database
from .intent_router import IntentDecision, route
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

    def __init__(self, settings: Settings, core: SupportCore, db: Database, operator: OperatorBridge, *, knowledge_base=None):
        if not settings.vk_group_token or not settings.vk_group_id:
            raise RuntimeError("VKAdapter requires VK_GROUP_TOKEN and VK_GROUP_ID")
        self.settings = settings
        self.core = core
        self.db = db
        self.operator = operator
        self.kb = knowledge_base
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=12.0))
        self._stopping = False
        self.group_name: str | None = None
        self.source_group_id: int | None = None
        self._profile_pages: list[dict[str, Any]] = []
        self._wall_pages: list[dict[str, Any]] = []

    async def close(self) -> None:
        self._stopping = True
        await self.client.aclose()

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

        await self.typing(user.peer_id)
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
            parts.append(f"\n#{row['id']} · {row['full_name']}\n{q}")
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

        self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="message", name=intent or "text")
        if intent:
            self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="button", name=intent)

        if await self._manager_command(user, text):
            return

        if intent == "start" or text.lower() in {"начать", "/start", "start"}:
            await self._welcome(user)
            return

        # A plain greeting is social contact, not an unanswered support request. Never escalate it.
        if not intent and text and is_greeting_only(text):
            greeting, _ = split_leading_greeting(text)
            self.db.track(user_id=user.user_id, peer_id=user.peer_id, kind="smalltalk", name="greeting")
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
        if typ == "message_new":
            await self._handle_message_new(update)
        elif typ == "message_event":
            await self._handle_message_event(update)

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
