from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.booking import BookingResult
from app.config import Settings, load_settings
from app.core import SupportCore
from app.db import Database
from app.intake import IntakeUser, SmartHandoffEngine, classify_transaction
from app.intent_router import route
from app.knowledge import KnowledgeBase
from app.knowledge_loop import knowledge_candidate_allowed
from app.operator import OperatorBridge
from app.responder import StrictResponder
from app.version import VERSION
from app.vk_adapter import VKAdapter, VKUser
from app.vk_keyboards import knowledge_candidate_keyboard, parent_intake_confirmation_keyboard

ROOT = Path(__file__).resolve().parent.parent


def settings(tmp: Path, **kw) -> Settings:
    base = dict(
        site_url="https://zvezdaglazov.ru/",
        kb_file=tmp / "knowledge.json",
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
        smart_handoff_enabled=True,
        smart_handoff_ttl_minutes=90,
        knowledge_loop_enabled=True,
    )
    base.update(kw)
    return Settings(**base)


def core_env(tmp: Path, s: Settings, db: Database):
    kb = KnowledgeBase(
        tmp / "missing.json", ROOT / "data" / "seed_faq.json", db,
        fallback_kb_file=ROOT / "data" / "knowledge_base.json", max_age_hours=1_000_000,
    )
    responder = StrictResponder(s, kb)
    return kb, SupportCore(responder)


class V60ReleaseTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(VERSION, "6.0.1")

    def test_env_flags_are_safe_off(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("SMART_HANDOFF_ENABLED=false", text)
        self.assertIn("SMART_HANDOFF_TTL_MINUTES=90", text)
        self.assertIn("KNOWLEDGE_LOOP_ENABLED=false", text)

    def test_load_settings_defaults_features_off(self):
        env = {"VK_GROUP_TOKEN": "x", "VK_GROUP_ID": "241267977"}
        for k in ["SMART_HANDOFF_ENABLED", "SMART_HANDOFF_TTL_MINUTES", "KNOWLEDGE_LOOP_ENABLED"]:
            env[k] = ""
        with patch.dict(os.environ, env, clear=False):
            # empty boolean env is false; invalid empty ttl is not allowed, so remove TTL only
            os.environ.pop("SMART_HANDOFF_TTL_MINUTES", None)
            s = load_settings()
        self.assertFalse(s.smart_handoff_enabled)
        self.assertFalse(s.knowledge_loop_enabled)
        self.assertEqual(s.smart_handoff_ttl_minutes, 90)

    def test_optional_feature_numeric_typos_do_not_crash_settings(self):
        env = {
            "VK_GROUP_TOKEN": "x", "VK_GROUP_ID": "241267977",
            "SMART_HANDOFF_TTL_MINUTES": "oops",
            "BOOKING_HOLD_MINUTES": "oops",
            "BOOKING_TIMEOUT_SECONDS": "oops",
        }
        with patch.dict(os.environ, env, clear=False):
            s = load_settings()
        self.assertEqual(s.smart_handoff_ttl_minutes, 90)
        self.assertEqual(s.booking_hold_minutes, 15)
        self.assertEqual(s.booking_timeout_seconds, 15.0)

    def test_keyboard_payloads(self):
        self.assertIn("intake_parent", parent_intake_confirmation_keyboard())
        self.assertIn("knowledge_loop", knowledge_candidate_keyboard(7))
        self.assertIn("ticket_id", knowledge_candidate_keyboard(7))


class ClassifierTests(unittest.TestCase):
    def test_refund(self): self.assertEqual(classify_transaction("Хочу вернуть путёвку, ребёнок заболел"), "refund")
    def test_transfer(self): self.assertEqual(classify_transaction("Хочу перенести ребёнка на другую смену"), "transfer")
    def test_payment(self): self.assertEqual(classify_transaction("У меня не проходит оплата"), "payment")
    def test_lost(self): self.assertEqual(classify_transaction("Ребёнок забыл вещи в лагере"), "lost_item")
    def test_lost_specific_item(self): self.assertEqual(classify_transaction("Ребёнок забыл куртку после 2 смены, помогите найти"), "lost_item")
    def test_together(self): self.assertEqual(classify_transaction("Хотим детей в один отряд"), "together")
    def test_food(self): self.assertEqual(classify_transaction("У ребёнка аллергия, куда сообщить про питание?"), "food")
    def test_medicine(self): self.assertEqual(classify_transaction("Ребёнок принимает лекарства, хочу передать их"), "medicine")
    def test_contact_child(self): self.assertEqual(classify_transaction("Не могу дозвониться до ребёнка"), "contact_child")
    def test_complaint(self): self.assertEqual(classify_transaction("Хочу пожаловаться, ребёнка обижают"), "complaint")
    def test_complaint_reverse_word_order(self): self.assertEqual(classify_transaction("Ребёнка обижают в отряде"), "complaint")
    def test_shift_availability(self): self.assertEqual(classify_transaction("Есть ли места на 2 смену?"), "shift_availability")
    def test_urgent(self): self.assertEqual(classify_transaction("Ребёнок не дышит, срочно"), "urgent_safety")
    def test_trauma_urgent(self): self.assertEqual(classify_transaction("Ребёнок получил серьёзную травму"), "urgent_safety")
    def test_policy_refund_not_hijacked(self): self.assertIsNone(classify_transaction("Какие условия возврата путёвки?"))
    def test_policy_transfer_not_hijacked(self): self.assertIsNone(classify_transaction("Можно ли перенести на другую смену?"))
    def test_normal_faq_not_hijacked(self): self.assertIsNone(classify_transaction("Какие документы нужны?"))
    def test_lost_item_policy_faq_not_hijacked(self): self.assertIsNone(classify_transaction("Что делать, если ребёнок потерял вещь?"))
    def test_medicine_policy_faq_not_hijacked(self): self.assertIsNone(classify_transaction("Как передать лекарства ребёнку?"))
    def test_contact_policy_faq_not_hijacked(self): self.assertIsNone(classify_transaction("Как связаться с ребёнком в лагере?"))
    def test_payment_failure_question_still_uses_intake(self): self.assertEqual(classify_transaction("Что делать, если оплата не проходит?"), "payment")

    def test_common_parent_phrasings_are_structured(self):
        cases = {
            "Хочу вернуть деньги за путёвку": "refund",
            "Хотим отказаться от смены и вернуть деньги": "refund",
            "Можно оформить возврат? ребёнок заболел": "refund",
            "Хочу поменять вторую смену на третью": "transfer",
            "Нам нужно перенести путёвку на другую смену": "transfer",
            "Можно нас перенести с 2 на 3 смену?": "transfer",
            "Оплатил, но подтверждение не пришло": "payment",
            "Платёж отклоняется": "payment",
            "С карты списали деньги, а путёвки нет": "payment",
            "Не можем найти куртку после лагеря": "lost_item",
            "У нас осталась сумка в корпусе": "lost_item",
            "Хотим чтобы Иван и Петя были в одном отряде": "together",
            "Пожалуйста поселите детей вместе": "together",
            "Нужно особое питание ребёнку": "food",
            "Ребёнок на медицинской диете, хочу предупредить лагерь": "food",
            "Нужно согласовать приём таблеток у ребёнка": "medicine",
            "У ребёнка астма, хочу связаться с медиком": "medicine",
            "Дочь не выходит на связь": "contact_child",
            "Срочно хочу связаться с сыном": "contact_child",
            "У сына конфликт с детьми": "complaint",
            "Есть проблема с вожатым, хочу сообщить": "complaint",
            "Остались путёвки на 3 смену?": "shift_availability",
            "На зимнюю смену места есть?": "shift_availability",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify_transaction(text), expected)

    def test_generic_safety_policy_questions_are_not_marked_as_live_emergencies(self):
        for text in [
            "Как обеспечена пожарная безопасность в лагере?",
            "Какие меры пожарной безопасности есть?",
            "Как вы предотвращаете насилие?",
            "Какие меры против насилия и буллинга?",
            "Как лагерь борется с буллингом?",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(classify_transaction(text))

    def test_real_fire_and_violence_incidents_still_escalate_urgently(self):
        for text in [
            "Пожар!",
            "У нас пожар в корпусе",
            "Загорелась комната",
            "Произошло насилие над ребёнком",
        ]:
            with self.subTest(text=text):
                self.assertEqual(classify_transaction(text), "urgent_safety")

    def test_personal_bullying_case_is_complaint_but_generic_policy_is_not(self):
        self.assertEqual(classify_transaction("У сына буллинг в отряде, хочу сообщить"), "complaint")
        self.assertIsNone(classify_transaction("Как лагерь борется с буллингом?"))

    def test_generic_policy_variants_stay_out_of_intake(self):
        for text in [
            "Как оформить возврат путёвки?",
            "Можно ли вернуть деньги за путёвку?",
            "Как перенести ребёнка на другую смену?",
            "Можно ли друзьям попасть в один отряд?",
            "У ребёнка постоянные лекарства, как их передавать?",
            "Как связаться с ребёнком в лагере?",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(classify_transaction(text))


class IntentRegressionTests(unittest.TestCase):
    def test_medicine_phrase_is_not_misread_as_food(self):
        decision = route("Как передать лекарства ребёнку?")
        self.assertEqual(decision.intent, "medicines")

    def test_plain_food_question_still_routes_to_food(self):
        self.assertEqual(route("Какая еда в лагере?").intent, "food")


class IntakeEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.s = settings(self.tmp)
        self.db = Database(self.tmp / "db.sqlite3")
        self.e = SmartHandoffEngine(self.s, self.db)
        self.u = IntakeUser(101, 101, "Иванова Анна", "anna")

    async def asyncTearDown(self): self.td.cleanup()

    async def test_refund_flow_and_restart_persistence(self):
        r = await self.e.handle_text(self.u, "Хочу вернуть путёвку на 2 смену, ребёнок заболел")
        self.assertTrue(r.handled)
        self.assertIn("ФИО ребёнка", r.text)
        # Simulate container restart: a new engine uses the same SQLite state.
        e2 = SmartHandoffEngine(self.s, self.db)
        r = await e2.handle_text(self.u, "Иванов Иван")
        self.assertIn("оплата/договор", r.text.lower())
        r = await e2.handle_text(self.u, "Иванова Анна")
        self.assertIn("телефон", r.text.lower())
        r = await e2.handle_text(self.u, "+7 912 345-67-89")
        self.assertEqual(r.state, "confirm_parent")
        self.assertIn("2 смена", r.text)
        self.assertIn("заболел", r.text.lower())

    async def test_transfer_two_shifts_extracted(self):
        r = await self.e.handle_text(self.u, "Хочу перенести ребёнка со 2 смены на 3 смену")
        self.assertIn("ФИО", r.text)
        row = self.db.get_intake_session(user_id=self.u.user_id)
        self.assertEqual(row["payload"]["fields"]["current_shift"], "2 смена")
        self.assertEqual(row["payload"]["fields"]["desired_shift"], "3 смена")

    async def test_same_transfer_shift_rejected(self):
        await self.e.handle_text(self.u, "Хочу перенести ребёнка со 2 смены")
        r = await self.e.handle_text(self.u, "2 смена")
        self.assertIn("совпадают", r.text)

    async def test_payment_context_not_asked_twice(self):
        r = await self.e.handle_text(self.u, "У меня не проходит оплата за 2 смену")
        self.assertIn("телефон", r.text.lower())
        row = self.db.get_intake_session(user_id=self.u.user_id)
        self.assertIn("не проходит оплат", row["payload"]["fields"]["payment_context"].lower())

    async def test_initial_card_number_is_never_persisted(self):
        card = "4111 1111 1111 1111"
        r = await self.e.handle_text(self.u, f"Не проходит оплата, карта {card}")
        self.assertEqual(r.state, "security_reject")
        self.assertIsNone(self.db.get_intake_session(user_id=self.u.user_id))
        with closing(sqlite3.connect(self.db.path)) as conn:
            blob = "\n".join(str(x[0]) for x in conn.execute("SELECT payload_json FROM support_intake_sessions").fetchall())
        self.assertNotIn("4111", blob)

    async def test_card_number_mid_form_rejected_and_not_saved(self):
        await self.e.handle_text(self.u, "У меня не проходит оплата")
        r = await self.e.handle_text(self.u, "4111 1111 1111 1111")
        self.assertIn("Не присылайте", r.text)
        row = self.db.get_intake_session(user_id=self.u.user_id)
        self.assertNotIn("4111", json.dumps(row["payload"], ensure_ascii=False))

    async def test_urgent_message_interrupts_active_form_and_redacts_payment_secret(self):
        await self.e.handle_text(self.u, "Хочу вернуть путёвку на 2 смену")
        self.assertTrue(self.db.get_intake_session(user_id=self.u.user_id))
        text = "Ребёнок не дышит, карта 4111 1111 1111 1111"
        self.assertTrue(self.e.candidate(text, user_id=self.u.user_id))
        r = await self.e.handle_text(self.u, text)
        self.assertEqual(r.state, "submit_immediate")
        self.assertIsNone(self.db.get_intake_session(user_id=self.u.user_id))
        self.assertNotIn("4111", r.submission["original_question"])
        self.assertIn("номер карты скрыт", r.submission["original_question"])

    async def test_unrelated_faq_does_not_consume_active_session(self):
        await self.e.handle_text(self.u, "Хочу вернуть путёвку на 2 смену")
        before = self.db.get_intake_session(user_id=self.u.user_id)
        self.assertFalse(self.e.candidate("Какие документы нужны?", user_id=self.u.user_id))
        after = self.db.get_intake_session(user_id=self.u.user_id)
        self.assertEqual(before["step"], after["step"])

    async def test_cancel_clears(self):
        await self.e.handle_text(self.u, "Хочу вернуть путёвку")
        r = await self.e.handle_text(self.u, "отмена")
        self.assertEqual(r.state, "cancelled")
        self.assertIsNone(self.db.get_intake_session(user_id=self.u.user_id))

    async def test_expired_session_is_removed(self):
        await self.e.handle_text(self.u, "Хочу вернуть путёвку")
        with self.db._conn() as conn:
            conn.execute("UPDATE support_intake_sessions SET expires_at=? WHERE user_id=?", ((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(), self.u.user_id))
        self.assertIsNone(self.db.get_intake_session(user_id=self.u.user_id))

    async def test_unknown_numbered_shift_does_not_trap_availability_form(self):
        r = await self.e.handle_text(self.u, "Есть места на 5 смену?")
        self.assertEqual(r.state, "collecting")
        self.assertIn("сколько лет", r.text.lower())
        row = self.db.get_intake_session(user_id=self.u.user_id)
        self.assertEqual(row["payload"]["fields"].get("shift"), "5 смена")

    async def test_parent_confirmation_is_atomic(self):
        await self.e.handle_text(self.u, "Есть места на 2 смену? Мне 10 лет, телефон +7 912 345-67-89")
        # It may still need age depending phrasing; complete deterministically.
        row = self.db.get_intake_session(user_id=self.u.user_id)
        if row and row["status"] != "confirm_parent":
            if row["step"] == "child_age": await self.e.handle_text(self.u, "10 лет")
            row = self.db.get_intake_session(user_id=self.u.user_id)
            if row and row["step"] == "phone": await self.e.handle_text(self.u, "+7 912 345-67-89")
        r1 = await self.e.parent_decision(self.u, confirm=True)
        r2 = await self.e.parent_decision(self.u, confirm=True)
        self.assertEqual(r1.state, "submit")
        self.assertEqual(r2.state, "stale")

    async def test_failed_submission_can_be_retried(self):
        await self.e.handle_text(self.u, "Есть места на 2 смену?")
        await self.e.handle_text(self.u, "10 лет")
        await self.e.handle_text(self.u, "+7 912 345-67-89")
        r = await self.e.parent_decision(self.u, confirm=True)
        self.assertEqual(r.state, "submit")
        self.e.submission_finished(self.u.user_id, success=False)
        self.assertEqual(self.db.get_intake_session(user_id=self.u.user_id)["status"], "confirm_parent")

    async def test_database_restart_recovers_claimed_submission(self):
        await self.e.handle_text(self.u, "Есть места на 2 смену?")
        await self.e.handle_text(self.u, "10 лет")
        await self.e.handle_text(self.u, "+7 912 345-67-89")
        claimed = self.db.claim_intake_submission(user_id=self.u.user_id)
        self.assertIsNotNone(claimed)
        self.assertEqual(self.db.get_intake_session(user_id=self.u.user_id)["status"], "submitting")
        # New Database instance = new bot process startup.
        db2 = Database(self.db.path)
        self.assertEqual(db2.get_intake_session(user_id=self.u.user_id)["status"], "confirm_parent")

    async def test_all_supported_forms_reach_parent_confirmation(self):
        cases = [
            ("refund", "Хочу вернуть путёвку на 2 смену, ребёнок заболел", ["Иванов Иван", "Иванова Анна", "+7 912 345-67-89"]),
            ("transfer", "Хочу перенести ребёнка со 2 смены на 3 смену", ["Иванов Иван", "+7 912 345-67-89"]),
            ("payment", "Не проходит оплата за 2 смену", ["+7 912 345-67-89"]),
            ("lost_item", "Ребёнок потерял вещь на 2 смене", ["Иванов Иван", "Чёрная толстовка Adidas", "Размер 152, подпись Иванов"]),
            ("together", "Хотим детей в один отряд на 2 смену", ["Иванов Иван и Петров Пётр"]),
            ("food", "У ребёнка аллергия, нужно питание на 2 смене", ["Иванов Иван", "+7 912 345-67-89"]),
            ("medicine", "Ребёнок принимает лекарства на 2 смене, хочу передать их", ["Иванов Иван", "+7 912 345-67-89"]),
            ("contact_child", "Не могу дозвониться до ребёнка на 2 смене", ["Иванов Иван", "+7 912 345-67-89", "Нужно срочно поговорить по семейному вопросу"]),
            ("complaint", "Хочу пожаловаться: ребёнка обижают на 2 смене", ["Иванов Иван", "Есть конфликт в отряде, прошу разобраться", "+7 912 345-67-89"]),
            ("shift_availability", "Есть ли места на 2 смену?", ["10 лет", "+7 912 345-67-89"]),
        ]
        for idx, (kind, first, answers) in enumerate(cases, start=1):
            with self.subTest(kind=kind):
                user = IntakeUser(1000 + idx, 1000 + idx, "Тест Родитель", None)
                r = await self.e.handle_text(user, first)
                self.assertTrue(r.handled)
                for answer in answers:
                    if r.state == "confirm_parent":
                        break
                    r = await self.e.handle_text(user, answer)
                self.assertEqual(r.state, "confirm_parent", (kind, r.text))
                self.assertTrue(r.needs_parent_confirmation)
                row = self.db.get_intake_session(user_id=user.user_id)
                self.assertEqual(row["intake_type"], kind)

    async def test_urgent_has_no_form_and_warns_112(self):
        r = await self.e.handle_text(self.u, "Ребёнок не дышит")
        self.assertEqual(r.state, "submit_immediate")
        self.assertEqual(r.submission["priority"], "urgent")
        self.assertIn("112", r.text)
        self.assertIsNone(self.db.get_intake_session(user_id=self.u.user_id))

    async def test_high_priority_contact_child(self):
        r = await self.e.handle_text(self.u, "Не могу дозвониться до ребёнка")
        self.assertTrue(r.handled)
        row = self.db.get_intake_session(user_id=self.u.user_id)
        self.assertEqual(row["payload"]["priority"], "high")


class DatabaseMigrationTests(unittest.TestCase):
    def test_v59_like_database_migrates_without_data_loss(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "legacy.sqlite3"
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT DEFAULT 'vk', user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, username TEXT, full_name TEXT, question TEXT NOT NULL, status TEXT DEFAULT 'open', support_message_id INTEGER, human_answer TEXT, escalation_reason TEXT, confidence REAL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, answered_at TEXT, closed_at TEXT);
                CREATE TABLE manual_faq(id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, answer TEXT NOT NULL, source TEXT DEFAULT 'approved_manager_knowledge', aliases_json TEXT DEFAULT '[]', kind TEXT DEFAULT 'permanent', expires_at TEXT, created_by INTEGER, active INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE answer_events(id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT DEFAULT 'vk', user_id INTEGER, chat_id INTEGER, username TEXT, full_name TEXT, question TEXT, faq_id TEXT, reason TEXT, confidence REAL, helpful INTEGER, feedback_at TEXT, escalated_ticket_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO tickets(user_id,chat_id,full_name,question) VALUES(1,1,'Parent','Old ticket');
                INSERT INTO manual_faq(question,answer) VALUES('Old q','Old a');
            """)
            con.commit(); con.close()
            db = Database(path)
            self.assertEqual(db.get_ticket(1)["question"], "Old ticket")
            self.assertEqual(db.manual_knowledge_count(), 1)
            self.assertEqual(db.intake_session_count(), 0)
            with db._conn() as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(tickets)")}
            self.assertTrue({"intake_type", "intake_payload_json", "intake_request_id", "priority", "knowledge_candidate_status", "knowledge_id"}.issubset(cols))


class OperatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_intake_ticket(self):
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d); s=settings(tmp); db=Database(tmp/"db.sqlite3"); op=OperatorBridge(s,db)
            sender=AsyncMock(return_value=999); op.set_vk_manager_sender(sender)
            tid=await op.escalate_intake(user_id=7,chat_id=7,username="p",full_name="Parent",question="Хочу вернуть",intake_type="refund",intake_title="Возврат",intake_payload={"x":1},priority="high",summary_text="• Смена: 2")
            self.assertIsNotNone(tid)
            row=db.get_ticket(tid)
            self.assertEqual(row["intake_type"],"refund")
            self.assertEqual(row["priority"],"high")
            self.assertIn("Smart Handoff", sender.await_args.args[0])

    async def test_intake_request_id_is_idempotent_across_retry(self):
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d); s=settings(tmp); db=Database(tmp/"db.sqlite3"); op=OperatorBridge(s,db)
            sender=AsyncMock(return_value=999); op.set_vk_manager_sender(sender)
            payload={"request_id":"req-123","fields":{}}
            kwargs=dict(user_id=7,chat_id=7,username=None,full_name="P",question="Q",intake_type="refund",intake_title="Возврат",intake_payload=payload,summary_text="x")
            a=await op.escalate_intake(**kwargs)
            b=await op.escalate_intake(**kwargs)
            self.assertEqual(a,b)
            self.assertEqual(sender.await_count,1)
            with db._conn() as c:
                count=c.execute("SELECT COUNT(*) FROM tickets WHERE intake_request_id='req-123'").fetchone()[0]
            self.assertEqual(count,1)

    async def test_failed_delivery_retry_reuses_same_ticket(self):
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d); s=settings(tmp); db=Database(tmp/"db.sqlite3"); op=OperatorBridge(s,db)
            sender=AsyncMock(side_effect=[RuntimeError("down"), 888]); op.set_vk_manager_sender(sender)
            payload={"request_id":"req-retry","fields":{}}
            kwargs=dict(user_id=7,chat_id=7,username=None,full_name="P",question="Q",intake_type="refund",intake_title="Возврат",intake_payload=payload,summary_text="x")
            first=await op.escalate_intake(**kwargs)
            self.assertIsNone(first)
            second=await op.escalate_intake(**kwargs)
            self.assertIsNotNone(second)
            with db._conn() as c:
                rows=c.execute("SELECT id,status,support_message_id FROM tickets WHERE intake_request_id='req-retry'").fetchall()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]["status"],"open")
            self.assertEqual(rows[0]["support_message_id"],888)

    async def test_manager_delivery_failure_marks_failed(self):
        with tempfile.TemporaryDirectory() as d:
            tmp=Path(d); s=settings(tmp); db=Database(tmp/"db.sqlite3"); op=OperatorBridge(s,db)
            op.set_vk_manager_sender(AsyncMock(side_effect=RuntimeError("down")))
            tid=await op.escalate_intake(user_id=7,chat_id=7,username=None,full_name="P",question="Q",intake_type="refund",intake_title="Refund",intake_payload={},summary_text="x")
            self.assertIsNone(tid)
            with db._conn() as c: row=c.execute("SELECT * FROM tickets ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(row["status"],"failed")


class KnowledgeLoopTests(unittest.TestCase):
    @staticmethod
    def ticket(**overrides):
        row={"question":"Можно ли ребёнку взять фен?","intake_type":None,"status":"answered","human_answer":"Да, фен можно взять. Хранить его нужно у воспитателя.","knowledge_candidate_status":None}
        row.update(overrides); return row

    def test_generic_rule_allowed(self):
        self.assertEqual(knowledge_candidate_allowed(self.ticket(), self.ticket()["human_answer"])[0], True)
    def test_intake_never_allowed(self):
        self.assertFalse(knowledge_candidate_allowed(self.ticket(intake_type="refund"), "Можно оформить у менеджера.")[0])
    def test_phone_blocked(self):
        self.assertFalse(knowledge_candidate_allowed(self.ticket(), "Позвоните +7 912 345-67-89")[0])
    def test_medical_blocked(self):
        self.assertFalse(knowledge_candidate_allowed(self.ticket(question="Можно ли дать ребёнку лекарство?"), "Передайте лекарство врачу.")[0])
    def test_dynamic_availability_blocked(self):
        self.assertFalse(knowledge_candidate_allowed(self.ticket(question="Есть места на 2 смену?"), "Сейчас места есть.")[0])
    def test_payment_blocked(self):
        self.assertFalse(knowledge_candidate_allowed(self.ticket(question="Как оплатить путёвку?"), "Оплатите по ссылке.")[0])


class AdapterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td=tempfile.TemporaryDirectory(); self.tmp=Path(self.td.name); self.s=settings(self.tmp)
        self.db=Database(self.tmp/"db.sqlite3"); self.kb,self.core=core_env(self.tmp,self.s,self.db)
        self.op=OperatorBridge(self.s,self.db); self.intake=SmartHandoffEngine(self.s,self.db)
        self.a=VKAdapter(self.s,self.core,self.db,self.op,knowledge_base=self.kb,intake=self.intake)
        self.user=VKUser(123456,123456,"Тест Родитель","parent")
        self.a._user=AsyncMock(return_value=self.user)
        self.a.send_message=AsyncMock(return_value=1)
        self.a.api=AsyncMock(return_value=1)
        self.op.set_vk_sender(AsyncMock(return_value=None))
        self.op.set_vk_manager_sender(AsyncMock(return_value=777))

    async def asyncTearDown(self): await self.a.close(); self.td.cleanup()

    async def test_first_contact_gets_welcome_then_intake_prompt(self):
        update={"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Добрый день, хочу вернуть путёвку на 2 смену"}}}
        await self.a._handle_message_new(update)
        self.assertGreaterEqual(self.a.send_message.await_count,2)
        self.assertIn("помощник детского лагеря", self.a.send_message.await_args_list[0].args[1].lower())
        self.assertIn("фио", self.a.send_message.await_args_list[1].args[1].lower())

    async def test_active_intake_allows_faq_and_keeps_session(self):
        await self.intake.handle_text(IntakeUser(self.user.user_id,self.user.peer_id,self.user.full_name,self.user.username),"Хочу вернуть путёвку на 2 смену")
        self.db.track(user_id=self.user.user_id,peer_id=self.user.peer_id,kind="message",name="seed")
        self.a.send_message.reset_mock()
        update={"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Какие документы нужны?"}}}
        await self.a._handle_message_new(update)
        self.assertTrue(self.db.get_intake_session(user_id=self.user.user_id))
        sent="\n".join(c.args[1] for c in self.a.send_message.await_args_list)
        self.assertIn("документ", sent.lower())

    async def test_existing_medicine_faq_is_not_hijacked_by_intake(self):
        self.db.track(user_id=self.user.user_id,peer_id=self.user.peer_id,kind="message",name="seed")
        self.a.send_message.reset_mock()
        await self.a._handle_message_new({"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Как передать лекарства ребёнку?"}}})
        self.assertIsNone(self.db.get_intake_session(user_id=self.user.user_id))
        sent="\n".join(c.args[1] for c in self.a.send_message.await_args_list)
        self.assertIn("лекарств", sent.lower())
        self.assertIn("сотруд", sent.lower())

    async def test_existing_lost_item_faq_is_not_hijacked_by_intake(self):
        self.db.track(user_id=self.user.user_id,peer_id=self.user.peer_id,kind="message",name="seed")
        self.a.send_message.reset_mock()
        await self.a._handle_message_new({"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Что делать, если ребёнок потерял вещь?"}}})
        self.assertIsNone(self.db.get_intake_session(user_id=self.user.user_id))
        sent="\n".join(c.args[1] for c in self.a.send_message.await_args_list)
        self.assertIn("найден", sent.lower())
        self.assertIn("офис", sent.lower())

    async def test_confirm_callback_creates_one_ticket(self):
        await self.intake.handle_text(IntakeUser(self.user.user_id,self.user.peer_id,self.user.full_name,self.user.username),"Есть места на 2 смену?")
        await self.intake.handle_text(IntakeUser(self.user.user_id,self.user.peer_id,self.user.full_name,self.user.username),"10 лет")
        await self.intake.handle_text(IntakeUser(self.user.user_id,self.user.peer_id,self.user.full_name,self.user.username),"+7 912 345-67-89")
        obj={"object":{"user_id":self.user.user_id,"peer_id":self.user.peer_id,"event_id":"e1","payload":{"action":"intake_parent","cmd":"confirm"}}}
        await self.a._handle_message_event(obj)
        with self.db._conn() as c: count=c.execute("SELECT COUNT(*) FROM tickets WHERE intake_type='shift_availability'").fetchone()[0]
        self.assertEqual(count,1)
        # stale second click must not duplicate ticket
        await self.a._handle_message_event(obj)
        with self.db._conn() as c: count2=c.execute("SELECT COUNT(*) FROM tickets WHERE intake_type='shift_availability'").fetchone()[0]
        self.assertEqual(count2,1)

    async def test_urgent_safety_bypasses_even_a_booking_candidate(self):
        class FakeBooking:
            def __init__(self): self.hit=False; self.abandoned=False; self.reason=None
            def candidate(self,text,*,user_id): return True
            def blocks_other_flow(self,user_id): return True
            async def abandon_for_other_flow(self,user_id,*,reason="switched_to_other_flow"):
                self.abandoned=True; self.reason=reason
            async def handle_text(self,user,text): self.hit=True; return BookingResult(True,"BOOKING",state="collecting")
            async def close(self): return None
        b=FakeBooking(); self.a.booking=b
        self.db.track(user_id=self.user.user_id,peer_id=self.user.peer_id,kind="message",name="seed")
        await self.a._handle_message_new({"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Ребёнок не дышит, срочно"}}})
        self.assertFalse(b.hit)
        self.assertTrue(b.abandoned)
        self.assertEqual(b.reason, "urgent_safety")
        with self.db._conn() as c:
            row=c.execute("SELECT intake_type,priority FROM tickets ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0],"urgent_safety")
        self.assertEqual(row[1],"urgent")

    async def test_urgent_safety_bypasses_booking_even_when_smart_handoff_is_disabled(self):
        class FakeBooking:
            def __init__(self): self.hit=False; self.abandoned=False; self.reason=None
            def candidate(self,text,*,user_id): return True
            def blocks_other_flow(self,user_id): return True
            async def abandon_for_other_flow(self,user_id,*,reason="switched_to_other_flow"):
                self.abandoned=True; self.reason=reason
            async def handle_text(self,user,text): self.hit=True; return BookingResult(True,"BOOKING",state="collecting")
            async def close(self): return None
        b=FakeBooking(); self.a.booking=b; self.a.intake=None
        self.db.track(user_id=self.user.user_id,peer_id=self.user.peer_id,kind="message",name="seed")
        self.a.send_message.reset_mock()
        await self.a._handle_message_new({"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Ребёнок не дышит, срочно"}}})
        self.assertFalse(b.hit)
        self.assertTrue(b.abandoned)
        self.assertEqual(b.reason, "urgent_safety")
        with self.db._conn() as c:
            row=c.execute("SELECT intake_type,priority FROM tickets ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0],"urgent_safety")
        self.assertEqual(row[1],"urgent")

    async def test_urgent_clears_stale_persisted_intake_when_feature_is_disabled(self):
        # Simulate a form started before SMART_HANDOFF_ENABLED was turned off.
        await self.intake.handle_text(
            IntakeUser(self.user.user_id, self.user.peer_id, self.user.full_name, self.user.username),
            "Хочу вернуть деньги за путёвку",
        )
        self.assertIsNotNone(self.db.get_intake_session(user_id=self.user.user_id))
        self.a.intake = None
        self.db.track(user_id=self.user.user_id, peer_id=self.user.peer_id, kind="message", name="seed")
        await self.a._handle_message_new({"object":{"message":{
            "from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Ребёнок не дышит, срочно"
        }}})
        self.assertIsNone(self.db.get_intake_session(user_id=self.user.user_id))
        with self.db._conn() as c:
            row=c.execute("SELECT intake_type,priority FROM tickets ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(row), ("urgent_safety", "urgent"))

    async def test_payment_secret_is_not_persisted_when_smart_handoff_is_disabled(self):
        self.a.intake=None
        self.db.track(user_id=self.user.user_id,peer_id=self.user.peer_id,kind="message",name="seed")
        self.a.send_message.reset_mock()
        secret="4111 1111 1111 1111"
        await self.a._handle_message_new({"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":f"Не проходит оплата, карта {secret}"}}})
        with self.db._conn() as c:
            count=c.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            blobs=[str(x[0] or "") for x in c.execute("SELECT payload_json FROM support_intake_sessions").fetchall()]
        self.assertEqual(count,0)
        self.assertNotIn("4111", "\n".join(blobs))
        sent="\n".join(c.args[1] for c in self.a.send_message.await_args_list)
        self.assertIn("Не присылайте номер банковской карты", sent)

    async def test_explicit_booking_fails_closed_to_human_when_booking_backend_is_disabled(self):
        self.a.booking=None
        self.db.track(user_id=self.user.user_id,peer_id=self.user.peer_id,kind="message",name="seed")
        self.a.send_message.reset_mock()
        await self.a._handle_message_new({"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Хочу забронировать беседку на 12 октября в 14:00"}}})
        with self.db._conn() as c:
            row=c.execute("SELECT escalation_reason,question FROM tickets ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0],"booking_not_enabled")
        self.assertIn("забронировать беседку", row[1].lower())
        sent="\n".join(c.args[1] for c in self.a.send_message.await_args_list)
        self.assertIn("не получается надёжно проверить календарь", sent.lower())

    async def test_disabled_booking_switch_clears_unfinished_intake(self):
        self.a.booking = None
        # Start a real Smart Handoff session first.
        await self.intake.handle_text(
            IntakeUser(self.user.user_id, self.user.peer_id, self.user.full_name, self.user.username),
            "Хочу вернуть деньги за путёвку",
        )
        self.assertIsNotNone(self.db.get_intake_session(user_id=self.user.user_id))
        self.db.track(user_id=self.user.user_id, peer_id=self.user.peer_id, kind="message", name="seed")
        self.a.send_message.reset_mock()
        await self.a._handle_message_new({"object":{"message":{
            "from_id":self.user.user_id,"peer_id":self.user.peer_id,
            "text":"Хочу забронировать беседку на 12 октября в 14:00"
        }}})
        self.assertIsNone(self.db.get_intake_session(user_id=self.user.user_id))
        with self.db._conn() as c:
            row=c.execute("SELECT escalation_reason FROM tickets ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "booking_not_enabled")

    async def test_booking_price_question_stays_faq_when_booking_backend_is_disabled(self):
        self.a.booking=None
        self.db.track(user_id=self.user.user_id,peer_id=self.user.peer_id,kind="message",name="seed")
        self.a.send_message.reset_mock()
        await self.a._handle_message_new({"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Сколько стоит аренда беседки?"}}})
        with self.db._conn() as c:
            count=c.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        self.assertEqual(count,0)
        sent="\n".join(c.args[1] for c in self.a.send_message.await_args_list)
        self.assertIn("2 700", sent)

    async def test_stale_intake_button_gets_answer_when_feature_is_disabled(self):
        self.a.intake = None
        self.a._event_answer = AsyncMock(return_value=None)
        self.a.send_message.reset_mock()
        await self.a._handle_message_event({"object":{
            "user_id":self.user.user_id,"peer_id":self.user.peer_id,"event_id":"stale",
            "payload":{"action":"intake_parent","cmd":"confirm"}
        }})
        self.a._event_answer.assert_awaited()
        self.assertTrue(self.a.send_message.await_count)
        self.assertIn("Smart Handoff сейчас отключён", self.a.send_message.await_args.args[1])

    async def test_switch_from_collecting_booking_to_smart_handoff_abandons_booking(self):
        class FakeBooking:
            def __init__(self): self.abandoned=False
            def candidate(self,text,*,user_id): return False
            def blocks_other_flow(self,user_id): return True
            async def abandon_for_other_flow(self,user_id,*,reason): self.abandoned=True
            async def close(self): return None
        b=FakeBooking(); self.a.booking=b
        self.db.track(user_id=self.user.user_id,peer_id=self.user.peer_id,kind="message",name="seed")
        await self.a._handle_message_new({"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Хочу вернуть путёвку"}}})
        self.assertTrue(b.abandoned)
        self.assertIsNotNone(self.db.get_intake_session(user_id=self.user.user_id))

    async def test_switch_from_smart_handoff_to_booking_clears_intake(self):
        await self.intake.handle_text(
            IntakeUser(self.user.user_id,self.user.peer_id,self.user.full_name,self.user.username),
            "Хочу вернуть путёвку",
        )
        self.assertIsNotNone(self.db.get_intake_session(user_id=self.user.user_id))
        class FakeBooking:
            def __init__(self): self.hit=False
            def candidate(self,text,*,user_id): return True
            async def handle_text(self,user,text): self.hit=True; return BookingResult(True,"BOOKING",state="collecting")
            async def close(self): return None
        b=FakeBooking(); self.a.booking=b
        self.db.track(user_id=self.user.user_id,peer_id=self.user.peer_id,kind="message",name="seed")
        await self.a._handle_message_new({"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Хочу забронировать беседку"}}})
        self.assertTrue(b.hit)
        self.assertIsNone(self.db.get_intake_session(user_id=self.user.user_id))

    async def test_booking_has_precedence_over_intake(self):
        class FakeBooking:
            def __init__(self): self.hit=False
            def candidate(self,text,*,user_id): return True
            async def handle_text(self,user,text): self.hit=True; return BookingResult(True,"BOOKING",state="collecting")
            async def close(self): return None
        b=FakeBooking(); self.a.booking=b
        self.db.track(user_id=self.user.user_id,peer_id=self.user.peer_id,kind="message",name="seed")
        await self.a._handle_message_new({"object":{"message":{"from_id":self.user.user_id,"peer_id":self.user.peer_id,"text":"Хочу забронировать беседку"}}})
        self.assertTrue(b.hit)
        self.assertIsNone(self.db.get_intake_session(user_id=self.user.user_id))

    async def test_knowledge_add_callback_saves_only_after_manager_choice(self):
        tid=self.db.create_ticket(user_id=9,chat_id=9,username=None,full_name="P",question="Можно ли ребёнку взять фен?")
        self.db.save_human_answer(tid,"Да, фен можно взять. Хранить его нужно у воспитателя.")
        self.db.set_ticket_knowledge_candidate(tid,"offered")
        manager=VKUser(self.s.vk_manager_user_id,self.s.vk_manager_user_id,"Manager",None)
        self.a._user=AsyncMock(return_value=manager)
        await self.a._handle_message_event({"object":{"user_id":manager.user_id,"peer_id":manager.peer_id,"event_id":"e","payload":{"action":"knowledge_loop","cmd":"add","ticket_id":tid}}})
        row=self.db.get_ticket(tid)
        self.assertEqual(row["knowledge_candidate_status"],"saved")
        self.assertEqual(self.db.manual_knowledge_count(),1)

    async def test_stale_knowledge_button_cannot_save_when_feature_disabled(self):
        tid=self.db.create_ticket(user_id=9,chat_id=9,username=None,full_name="P",question="Можно ли ребёнку взять фен?")
        self.db.save_human_answer(tid,"Да, фен можно взять. Хранить его нужно у воспитателя.")
        self.db.set_ticket_knowledge_candidate(tid,"offered")
        manager=VKUser(self.s.vk_manager_user_id,self.s.vk_manager_user_id,"Manager",None)
        self.a._user=AsyncMock(return_value=manager)
        object.__setattr__(self.s, "knowledge_loop_enabled", False)
        await self.a._handle_message_event({"object":{"user_id":manager.user_id,"peer_id":manager.peer_id,"event_id":"e","payload":{"action":"knowledge_loop","cmd":"add","ticket_id":tid}}})
        self.assertEqual(self.db.manual_knowledge_count(),0)
        self.assertEqual(self.db.get_ticket(tid)["knowledge_candidate_status"],"offered")

    async def test_knowledge_edit_flow(self):
        tid=self.db.create_ticket(user_id=9,chat_id=9,username=None,full_name="P",question="Можно ли ребёнку взять фен?")
        self.db.save_human_answer(tid,"Фен разрешён.")
        self.db.set_ticket_knowledge_candidate(tid,"offered")
        manager=VKUser(self.s.vk_manager_user_id,self.s.vk_manager_user_id,"Manager",None)
        self.a._user=AsyncMock(return_value=manager)
        await self.a._handle_message_event({"object":{"user_id":manager.user_id,"peer_id":manager.peer_id,"event_id":"e","payload":{"action":"knowledge_loop","cmd":"edit","ticket_id":tid}}})
        self.assertEqual(self.db.get_ticket(tid)["knowledge_candidate_status"],"editing")
        handled=await self.a._manager_command(manager,"Да, фен можно взять. Хранить его нужно у воспитателя.")
        self.assertTrue(handled)
        self.assertEqual(self.db.get_ticket(tid)["knowledge_candidate_status"],"saved")

    async def test_knowledge_edit_menu_cancels_candidate(self):
        tid=self.db.create_ticket(user_id=9,chat_id=9,username=None,full_name="P",question="Можно ли ребёнку взять фен?")
        self.db.save_human_answer(tid,"Фен разрешён.")
        self.db.set_ticket_knowledge_candidate(tid,"editing")
        manager=VKUser(self.s.vk_manager_user_id,self.s.vk_manager_user_id,"Manager",None)
        self.db.set_admin_state(user_id=manager.user_id,state="knowledge_loop_edit",payload={"ticket_id":tid})
        await self.a._manager_command(manager,"#меню")
        self.assertEqual(self.db.get_ticket(tid)["knowledge_candidate_status"],"skipped")

    async def test_failed_manager_delivery_releases_confirmation(self):
        self.op.set_vk_manager_sender(AsyncMock(side_effect=RuntimeError("down")))
        u=IntakeUser(self.user.user_id,self.user.peer_id,self.user.full_name,self.user.username)
        await self.intake.handle_text(u,"Есть места на 2 смену?")
        await self.intake.handle_text(u,"10 лет")
        await self.intake.handle_text(u,"+7 912 345-67-89")
        r=await self.intake.parent_decision(u,confirm=True)
        await self.a._send_intake_result(self.user,r)
        row=self.db.get_intake_session(user_id=self.user.user_id)
        self.assertEqual(row["status"],"confirm_parent")


if __name__ == "__main__":
    unittest.main()
