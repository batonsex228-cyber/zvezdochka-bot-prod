from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import Settings
from app.core import SupportCore
from app.db import Database
from app.intent_router import route
from app.knowledge import KnowledgeBase
from app.operator import OperatorBridge
from app.responder import StrictResponder, looks_dynamic_availability, manual_knowledge_allowed
from app.vk_adapter import VKAdapter, VKUser
from app.vk_keyboards import admin_keyboard, knowledge_menu_keyboard, knowledge_preview_keyboard, knowledge_ttl_keyboard
from app.texts import escalation_text
from app.version import VERSION

ROOT = Path(__file__).resolve().parent.parent


def settings(tmp: Path) -> Settings:
    return Settings(
        site_url="https://zvezdaglazov.ru/",
        kb_file=tmp / "missing.json",
        db_file=tmp / "db.sqlite3",
        crawl_max_pages=40,
        openai_api_key=None,
        openai_model="gpt-5.6-luna",
        faq_min_score=0.72,
        retrieval_min_score=0.48,
        camp_name="ДЗЛ «Звёздочка»",
        debug=False,
        show_source_links=True,
        auto_refresh_kb=False,
        kb_refresh_hours=6,
        kb_max_age_hours=1_000_000,
        vk_group_token="test-token",
        vk_group_id=241267977,
        vk_manager_user_id=544188872,
        vk_source_domain="zvezdochkaooorazvitie",
    )


def env(*, fallback=True):
    td = tempfile.TemporaryDirectory()
    tmp = Path(td.name)
    db = Database(tmp / "db.sqlite3")
    s = settings(tmp)
    kb = KnowledgeBase(
        tmp / "missing.json",
        ROOT / "data" / "seed_faq.json",
        db,
        fallback_kb_file=(ROOT / "data" / "knowledge_base.json") if fallback else None,
        max_age_hours=1_000_000,
    )
    responder = StrictResponder(s, kb)
    core = SupportCore(responder)
    return td, tmp, db, s, kb, responder, core


class VersionAndLiveSeedTests(unittest.TestCase):
    def test_version_is_57(self):
        self.assertEqual(VERSION, "5.7.3")

    def test_live_dialog_seed_can_work_without_website_snapshot(self):
        td, tmp, db, s, kb, responder, core = env(fallback=False)
        try:
            self.assertFalse(kb.is_fresh)
            faq = kb.get_faq_by_id("contract-documents-live")
            self.assertIsNotNone(faq)
            self.assertIn("паспорт родителя", faq.answer)
        finally:
            td.cleanup()

    def test_phone_policy_has_review_expiry(self):
        payload = json.loads((ROOT / "data" / "seed_faq.json").read_text(encoding="utf-8"))
        item = next(x for x in payload["faq"] if x["id"] == "phone-custody-live")
        self.assertFalse(item["requires_snapshot"])
        self.assertTrue(item["valid_until"].startswith("2026-10-01"))


class LiveLanguageRouterTests(unittest.TestCase):
    def test_arrival_first_time_phrase(self):
        self.assertEqual(route("Едем первый раз, во сколько завтра заезд в лагерь?").intent, "arrival")

    def test_arrival_with_shift_number_is_arrival_not_shift_card(self):
        self.assertEqual(route("Во сколько заезд 1 смены?").intent, "arrival")

    def test_contract_documents(self):
        self.assertEqual(route("Хочу подойти оформить договор. Какие документы необходимо взять?").intent, "contract_documents")

    def test_phone_custody(self):
        self.assertEqual(route("Забирают ли в вашем лагере телефоны?").intent, "phone_policy")

    def test_ndfl(self):
        self.assertEqual(route("Вы выдаете документы для возврата НДФЛ за путевку?").intent, "ndfl_documents")

    def test_gazebo_free_dates_never_become_extra_services(self):
        self.assertIsNone(route("Подскажите, какие есть свободные даты на беседку в субботу?").intent)

    def test_gazebo_availability_keeps_dynamic_entity(self):
        d = route("Свободна ли беседка на 13 сентября?")
        self.assertIsNone(d.intent)
        self.assertEqual(d.entity, "extra_service_dynamic")

    def test_date_only_followup_after_extra_services_stays_dynamic(self):
        d = route("А на 13 сентября?", {"topic": "extra_services", "entity": ""})
        self.assertIsNone(d.intent)
        self.assertEqual(d.entity, "extra_service_dynamic")

    def test_current_shift_ticket_availability_never_becomes_price(self):
        self.assertIsNone(route("Не осталось ли путевок на 5 смену?").intent)

    def test_current_first_shift_ticket_availability_never_becomes_shift_card(self):
        self.assertIsNone(route("Путевки на 1 смену еще есть?").intent)

    def test_email_with_word_documents_is_contacts(self):
        self.assertEqual(route("Напишите вашу почту чтобы дослать документы для путевки").intent, "contacts")

    def test_multichild_special_discount_escalates(self):
        self.assertIsNone(route("Есть ли скидки многодетным родителям?").intent)

    def test_installment_escalates(self):
        self.assertIsNone(route("А в рассрочку больше нельзя будет?").intent)

    def test_exact_visit_days_escalate(self):
        self.assertIsNone(route("В какие дни можно навещать детей?").intent)

    def test_combined_arrival_and_visit_question_escalates_whole_message(self):
        self.assertIsNone(route("Во сколько завтра заезд? И в какие дни можно навещать детей?").intent)

    def test_new_year_future_offer_uses_dated_live_fact(self):
        self.assertEqual(route("Будут ли предложения для однодневного выезда классом на Новый год?").intent, "new_year_offer")

    def test_detailed_class_overnight_package_escalates(self):
        self.assertIsNone(route("Есть программа с ночевкой для класса, что входит и какая стоимость?").intent)


class SafetyAndKnowledgeTests(unittest.TestCase):
    def test_gazebo_availability_detector(self):
        self.assertTrue(looks_dynamic_availability("13 сентября есть свободная беседка?"))

    def test_gazebo_free_li_phrase_is_dynamic(self):
        self.assertTrue(looks_dynamic_availability("Свободна ли беседка на 13 сентября?"))
        self.assertTrue(looks_dynamic_availability("Есть ли свободная беседка на 13 сентября?"))

    def test_gazebo_handoff_copy_says_it_will_be_checked(self):
        msg = escalation_text("Свободна ли беседка на 13 сентября?", "dynamic_availability", delivered=True)
        self.assertIn("уточню у специалиста", msg)
        self.assertIn("свободна ли беседка", msg)
        self.assertIn("отправил Ваш запрос", msg)

    def test_dynamic_handoff_without_manager_channel_is_honest(self):
        msg = escalation_text("Свободна ли беседка завтра?", "dynamic_availability", delivered=False)
        self.assertIn("не могу самостоятельно связать", msg)

    def test_permanent_price_is_rejected(self):
        ok, reason = manual_knowledge_allowed("Сколько стоит путёвка?", kind="permanent")
        self.assertFalse(ok)
        self.assertEqual(reason, "volatile_requires_temporary")

    def test_temporary_price_is_allowed(self):
        ok, reason = manual_knowledge_allowed("Сколько стоит путёвка?", kind="temporary")
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_even_temporary_current_availability_is_rejected(self):
        ok, reason = manual_knowledge_allowed("Есть свободные беседки на 13 сентября?", kind="temporary")
        self.assertFalse(ok)
        self.assertEqual(reason, "dynamic_availability")

    def test_manual_alias_available_immediately(self):
        td, tmp, db, s, kb, responder, core = env()
        try:
            kid = db.add_manual_knowledge(
                question="Можно ли взять настольную игру?",
                answer="Да, можно.",
                aliases=["Настолки можно?"],
                kind="permanent",
                created_by=1,
            )
            self.assertIsNotNone(kid)
            match = kb.find_faq("Настолки можно?")
            self.assertIsNotNone(match)
            self.assertTrue(match.source.startswith("approved_manager_knowledge"))
        finally:
            td.cleanup()

    def test_expired_manual_knowledge_is_not_used(self):
        td, tmp, db, s, kb, responder, core = env()
        try:
            db.add_manual_knowledge(
                question="Временный вопрос",
                answer="Временный ответ",
                kind="temporary",
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
            self.assertFalse(any(x.get("question") == "Временный вопрос" for x in db.list_manual_faq()))
        finally:
            td.cleanup()

    def test_deleted_manual_knowledge_is_not_used(self):
        td, tmp, db, s, kb, responder, core = env()
        try:
            kid = db.add_manual_knowledge(question="Тестовый вопрос менеджера", answer="Тестовый ответ")
            self.assertTrue(db.delete_manual_knowledge(kid))
            self.assertFalse(any(x.get("question") == "Тестовый вопрос менеджера" for x in db.list_manual_faq()))
            rows = db.list_manual_knowledge()
            self.assertFalse(rows[0]["active"])
        finally:
            td.cleanup()


class LiveAnswerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.tmp, self.db, self.s, self.kb, self.responder, self.core = env(fallback=False)

    async def asyncTearDown(self):
        self.td.cleanup()

    async def test_contract_documents_answer(self):
        r = await self.core.process("Хочу оформить договор, какие документы взять?", intent="contract_documents")
        self.assertTrue(r.supported)
        self.assertIn("паспорт родителя", r.presented.text)

    async def test_phone_policy_answer(self):
        r = await self.core.process("Забирают ли телефоны?", intent="phone_policy")
        self.assertTrue(r.supported)
        self.assertIn("16:30", r.presented.text)

    async def test_ndfl_answer(self):
        r = await self.core.process("Документы на НДФЛ", intent="ndfl_documents")
        self.assertTrue(r.supported)
        self.assertIn("ФИО ребёнка", r.presented.text)

    async def test_new_year_current_temporary_answer(self):
        r = await self.core.process("Будут ли программы на Новый год?", intent="new_year_offer")
        self.assertTrue(r.supported)
        self.assertIn("после 22 сентября", r.presented.text)

    async def test_dynamic_gazebo_question_never_uses_static_extra_services(self):
        r = await self.core.process("Есть свободная беседка 13 сентября?")
        self.assertFalse(r.supported)
        self.assertEqual(r.answer.reason, "dynamic_availability")

    async def test_context_forced_dynamic_intent_escalates_date_only_followup(self):
        r = await self.core.process("А на 13 сентября?", intent="dynamic_availability")
        self.assertFalse(r.supported)
        self.assertEqual(r.answer.reason, "dynamic_availability")


class KnowledgeKeyboardTests(unittest.TestCase):
    def test_admin_has_knowledge(self):
        self.assertIn("База знаний", admin_keyboard())

    def test_knowledge_keyboards_are_valid(self):
        for raw in [knowledge_menu_keyboard(), knowledge_ttl_keyboard(), knowledge_preview_keyboard()]:
            self.assertIn("buttons", json.loads(raw))


class ManagerKnowledgeFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.tmp, self.db, self.s, self.kb, self.responder, self.core = env()
        self.op = OperatorBridge(self.s, self.db)
        self.a = VKAdapter(self.s, self.core, self.db, self.op, knowledge_base=self.kb)
        self.a.send_message = AsyncMock(return_value=1)
        self.user = VKUser(self.s.vk_manager_user_id, self.s.vk_manager_user_id, "Manager", None)

    async def asyncTearDown(self):
        await self.a.close()
        self.td.cleanup()

    async def test_start_permanent_knowledge(self):
        await self.a._start_knowledge(self.user, "permanent")
        state = self.db.get_admin_state(user_id=self.user.user_id)
        self.assertEqual(state[0], "kb_question")

    async def test_text_flow_reaches_preview(self):
        await self.a._start_knowledge(self.user, "permanent")
        self.assertTrue(await self.a._manager_command(self.user, "Можно ли взять настольную игру?"))
        self.assertTrue(await self.a._manager_command(self.user, "Да, можно."))
        self.assertTrue(await self.a._manager_command(self.user, "Настолки можно? | Можно настольную игру?"))
        state = self.db.get_admin_state(user_id=self.user.user_id)
        self.assertEqual(state[0], "kb_preview")
        self.assertEqual(len(state[1]["aliases"]), 2)

    async def test_dynamic_availability_cannot_be_added(self):
        await self.a._start_knowledge(self.user, "temporary")
        self.assertTrue(await self.a._manager_command(self.user, "Есть свободная беседка завтра?"))
        self.assertIsNone(self.db.get_admin_state(user_id=self.user.user_id))
        self.assertEqual(self.db.manual_knowledge_count(), 0)

    async def test_knowledge_list_command(self):
        self.db.add_manual_knowledge(question="Можно взять настольную игру?", answer="Да")
        self.assertTrue(await self.a._manager_command(self.user, "#знания"))
        text = self.a.send_message.await_args.args[1]
        self.assertIn("БАЗА ЗНАНИЙ", text)
        self.assertIn("настольную игру", text)

    async def test_save_callback_persists_without_restart(self):
        self.db.set_admin_state(
            user_id=self.user.user_id,
            state="kb_preview",
            payload={
                "kind":"permanent",
                "question":"Можно ли взять настольную игру?",
                "answer":"Да, можно.",
                "aliases":["Настолки можно?"],
            },
        )
        self.a._user = AsyncMock(return_value=self.user)
        self.a._event_answer = AsyncMock()
        await self.a._handle_message_event({
            "object": {
                "peer_id": self.user.peer_id,
                "user_id": self.user.user_id,
                "event_id": "evt",
                "payload": {"action":"knowledge","cmd":"save"},
            }
        })
        self.assertEqual(self.db.manual_knowledge_count(), 1)
        self.assertIsNone(self.db.get_admin_state(user_id=self.user.user_id))
        self.assertEqual(self.kb.find_faq("Настолки можно?").source, "approved_manager_knowledge")
