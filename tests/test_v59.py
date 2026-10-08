from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from app.booking import BookingAPIError, BookingEngine, BookingUser
from app.config import Settings
from app.db import Database
from app.version import VERSION
from app.vk_adapter import VKAdapter, VKUser
from app.vk_keyboards import manager_booking_keyboard, parent_booking_confirmation_keyboard

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
        bookings_enabled=True,
        booking_api_url="https://example.invalid/exec",
        booking_api_secret="test-secret",
        booking_timezone="Europe/Samara",
        booking_hold_minutes=15,
    )
    base.update(kw)
    return Settings(**base)


class FakeBookingBackend:
    def __init__(self, *, occupied=False, corpus=False):
        self.occupied = occupied
        self.corpus = corpus
        self.calls = []
        self.closed = False
        self.booking_id = "ZV-TEST1234"
        self.submitted = False

    async def call(self, action, payload=None):
        payload = payload or {}
        self.calls.append((action, payload))
        if action == "get_service":
            key = payload.get("service_key")
            if key == "gazebo":
                return {
                    "found": True, "enabled": True, "service_key": "gazebo",
                    "service_name": "Беседка", "resource_key": "gazebo-1",
                    "resource_name": "Беседка", "mode": "hourly",
                    "min_duration_minutes": 60, "max_duration_minutes": 720,
                    "max_guests": 50,
                }
            if key == "corpus" and self.corpus:
                return {
                    "found": True, "enabled": True, "service_key": "corpus",
                    "service_name": "Аренда корпуса", "resource_key": "corpus-1",
                    "resource_name": "Корпус", "mode": "date_range",
                    "max_guests": 100,
                }
            return {"found": False, "enabled": False}
        if action == "create_hold":
            if self.occupied:
                return {"available": False, "reason": "occupied", "alternatives": []}
            return {
                "available": True, "booking_id": self.booking_id,
                "expires_at": "2030-10-12T10:00:00+04:00", "resource_name": "Беседка",
            }
        if action == "submit_booking":
            self.submitted = True
            return {"submitted": True, "booking_id": self.booking_id}
        if action == "manager_decision":
            return {
                "updated": True,
                "booking_id": self.booking_id,
                "vk_user_id": "123456",
                "vk_peer_id": "123456",
                "status": payload.get("decision"),
            }
        if action == "cancel_booking":
            return {"cancelled": True}
        if action == "health":
            return {"healthy": True}
        raise AssertionError(action)

    async def close(self):
        self.closed = True


class FailingBookingBackend:
    async def call(self, action, payload=None):
        raise BookingAPIError("simulated calendar outage")

    async def close(self):
        return None


class BookingEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.db = Database(self.tmp / "db.sqlite3")
        self.backend = FakeBookingBackend()
        self.engine = BookingEngine(settings(self.tmp), self.db, backend=self.backend)
        self.user = BookingUser(123456, 123456, "Тест Родитель", "test_parent")

    async def asyncTearDown(self):
        await self.engine.close()
        self.td.cleanup()

    async def test_full_gazebo_flow(self):
        r1 = await self.engine.handle_text(
            self.user,
            "Хочу забронировать беседку 12.10.2030 в 14:00 на 3 часа, нас 15 человек",
        )
        self.assertTrue(r1.handled)
        self.assertIn("ФИО", r1.text)
        self.assertTrue(any(a == "create_hold" for a, _ in self.backend.calls))

        r2 = await self.engine.handle_text(self.user, "Иванова Анна Петровна")
        self.assertIn("номер телефона", r2.text)

        r3 = await self.engine.handle_text(self.user, "+7 912 345-67-89")
        self.assertEqual(r3.state, "confirm_parent")
        self.assertIn("12 октября 2030", r3.text)
        self.assertIn("14:00–17:00", r3.text)
        self.assertIn("15 человек", r3.text)

        r4 = await self.engine.parent_decision(self.user, confirm=True)
        self.assertEqual(r4.state, "pending_manager")
        self.assertEqual(r4.booking_id, self.backend.booking_id)
        self.assertIn("ожидает подтверждения", r4.text)
        self.assertIn("Новая заявка", r4.manager_text)
        self.assertTrue(self.backend.submitted)

        r5 = await self.engine.manager_decision(booking_id=self.backend.booking_id, manager_id=544188872, approve=True)
        self.assertEqual(r5["decision"], "confirmed")
        self.assertIsNone(self.db.get_booking_session(user_id=self.user.user_id))

    async def test_impossible_date_is_rejected_before_backend_hold(self):
        r = await self.engine.handle_text(self.user, "Хочу беседку 31 сентября 2030 в 14:00 на 2 часа")
        self.assertTrue(r.handled)
        self.assertIn("Такой даты нет", r.text)
        self.assertFalse(any(a == "create_hold" for a, _ in self.backend.calls))

    async def test_occupied_slot_is_never_presented_as_free(self):
        self.backend.occupied = True
        r = await self.engine.handle_text(self.user, "Беседка 12 октября 2030 в 14:00 на 2 часа, 10 человек")
        self.assertTrue(r.handled)
        self.assertEqual(r.state, "unavailable")
        self.assertIn("уже занято", r.text)

    async def test_existing_booking_cancel_or_change_never_starts_a_new_booking(self):
        for text in [
            "Хочу отменить бронь беседки",
            "Нужно изменить время брони беседки",
            "Хочу перенести бронь корпуса на другую дату",
        ]:
            with self.subTest(text=text):
                self.backend.calls.clear()
                self.db.clear_booking_session(user_id=self.user.user_id)
                r = await self.engine.handle_text(self.user, text)
                self.assertTrue(r.handled)
                self.assertEqual(r.state, "escalate")
                self.assertEqual(r.error_reason, "booking_management_requires_manager")
                self.assertFalse(any(a == "create_hold" for a, _ in self.backend.calls))
                self.assertIsNone(self.db.get_booking_session(user_id=self.user.user_id))

    async def test_cancel_releases_hold(self):
        await self.engine.handle_text(self.user, "Беседка 12 октября 2030 в 14:00 на 2 часа, 10 человек")
        r = await self.engine.handle_text(self.user, "отмена")
        self.assertEqual(r.state, "cancelled")
        self.assertTrue(any(a == "cancel_booking" for a, _ in self.backend.calls))
        self.assertIsNone(self.db.get_booking_session(user_id=self.user.user_id))


    async def test_no_hold_until_guest_count_is_known(self):
        r = await self.engine.handle_text(self.user, "Беседка 12 октября 2030 в 14:00 на 2 часа")
        self.assertEqual(r.state, "collecting")
        self.assertIn("Сколько", r.text)
        self.assertFalse(any(a == "create_hold" for a, _ in self.backend.calls))

        r2 = await self.engine.handle_text(self.user, "10")
        self.assertIn("ФИО", r2.text)
        self.assertTrue(any(a == "create_hold" for a, _ in self.backend.calls))

    async def test_daypart_phrase_na_dva_chasa_dnya_means_14_00(self):
        r = await self.engine.handle_text(
            self.user, "Хочу беседку 12 октября 2030 на 2 часа дня, на 3 часа, 10 человек"
        )
        self.assertIn("ФИО", r.text)
        hold = next(payload for action, payload in self.backend.calls if action == "create_hold")
        self.assertIn("T14:00:00", hold["start_at"])
        self.assertIn("T17:00:00", hold["end_at"])

    async def test_time_range_s_14_do_17_is_parsed(self):
        r = await self.engine.handle_text(self.user, "Беседка 12 октября 2030 с 14 до 17, 10 человек")
        self.assertIn("ФИО", r.text)
        hold = next(payload for action, payload in self.backend.calls if action == "create_hold")
        self.assertIn("T14:00:00", hold["start_at"])
        self.assertIn("T17:00:00", hold["end_at"])

    async def test_phone_dashes_are_not_reparsed_as_date(self):
        await self.engine.handle_text(
            self.user, "Хочу беседку 12.10.2030 в 14:00 на 3 часа, 15 человек"
        )
        await self.engine.handle_text(self.user, "Иванова Анна Петровна")
        r = await self.engine.handle_text(self.user, "+7 912 345-67-89")
        self.assertEqual(r.state, "confirm_parent")
        self.assertIn("12 октября 2030", r.text)

    async def test_backend_outage_fails_closed(self):
        engine = BookingEngine(settings(self.tmp), self.db, backend=FailingBookingBackend())
        try:
            r = await engine.handle_text(self.user, "Беседка 12 октября 2030 в 14:00 на 2 часа, 10 человек")
            self.assertTrue(r.handled)
            self.assertEqual(r.state, "escalate")
            self.assertIn(r.error_reason, {"booking_service_not_configured", "booking_backend_unavailable"})
        finally:
            await engine.close()


    async def test_past_time_today_is_rejected_before_backend_hold(self):
        fixed = datetime(2030, 10, 12, 16, 0, tzinfo=ZoneInfo("Europe/Samara"))
        self.engine._now = lambda: fixed
        r = await self.engine.handle_text(self.user, "Хочу беседку сегодня в 14:00 на 2 часа, 10 человек")
        self.assertTrue(r.handled)
        self.assertEqual(r.state, "collecting")
        self.assertIn("время уже прошло", r.text)
        self.assertFalse(any(a == "create_hold" for a, _ in self.backend.calls))

    async def test_stale_parent_callback_cannot_fake_cancel_or_confirm(self):
        r = await self.engine.parent_decision(self.user, confirm=False)
        self.assertTrue(r.handled)
        self.assertEqual(r.state, "stale")
        self.assertIn("уже не ожидает", r.text)
        self.assertFalse(any(a == "cancel_booking" for a, _ in self.backend.calls))

    async def test_abandon_for_other_flow_releases_hold_and_clears_collecting_session(self):
        self.db.save_booking_session(
            user_id=self.user.user_id, peer_id=self.user.peer_id, step="confirm_parent", status="active",
            payload={"service_key":"gazebo","booking_id":"ZV-HOLD","start_date":"2030-10-12"},
            external_booking_id="ZV-HOLD", ttl_minutes=60,
        )
        self.assertTrue(self.engine.blocks_other_flow(self.user.user_id))
        await self.engine.abandon_for_other_flow(self.user.user_id, reason="switched_to_smart_handoff")
        self.assertIsNone(self.db.get_booking_session(user_id=self.user.user_id))
        cancel = [p for a,p in self.backend.calls if a == "cancel_booking"]
        self.assertTrue(cancel)
        self.assertEqual(cancel[-1]["booking_id"], "ZV-HOLD")

    async def test_pending_manager_booking_does_not_block_other_flow(self):
        self.db.save_booking_session(
            user_id=self.user.user_id, peer_id=self.user.peer_id, step="pending_manager", status="pending_manager",
            payload={"service_key":"gazebo","booking_id":"ZV-PENDING"}, external_booking_id="ZV-PENDING", ttl_minutes=60,
        )
        self.assertFalse(self.engine.blocks_other_flow(self.user.user_id))
        await self.engine.abandon_for_other_flow(self.user.user_id, reason="unrelated_support")
        self.assertIsNotNone(self.db.get_booking_session(user_id=self.user.user_id))
        self.assertFalse(any(a == "cancel_booking" and p.get("booking_id") == "ZV-PENDING" for a,p in self.backend.calls))

    async def test_active_booking_does_not_hijack_strong_unrelated_faq_or_handoff(self):
        self.db.save_booking_session(
            user_id=self.user.user_id, peer_id=self.user.peer_id, step="start_date", status="active",
            payload={"service_key": "gazebo"}, external_booking_id=None, ttl_minutes=60,
        )
        self.assertFalse(self.engine.candidate("Какие документы нужны ребёнку?", user_id=self.user.user_id))
        self.assertFalse(self.engine.candidate("Хочу вернуть путёвку", user_id=self.user.user_id))
        self.assertTrue(self.engine.candidate("12 октября 2030", user_id=self.user.user_id))

    async def test_pending_manager_does_not_hijack_unrelated_faq_question(self):
        self.db.save_booking_session(
            user_id=self.user.user_id, peer_id=self.user.peer_id, step="pending_manager", status="pending_manager",
            payload={"booking_id": "ZV-TEST1234", "service_key": "gazebo"}, external_booking_id="ZV-TEST1234", ttl_minutes=60,
        )
        self.assertFalse(self.engine.candidate("Какие документы нужны ребёнку?", user_id=self.user.user_id))
        self.assertTrue(self.engine.candidate("Отменить бронь", user_id=self.user.user_id))
        self.assertTrue(self.engine.candidate("Что с моей заявкой?", user_id=self.user.user_id))

    async def test_booking_health_check(self):
        r = await self.engine.health()
        self.assertTrue(r["healthy"])

    async def test_plain_gazebo_information_question_is_not_hijacked_by_booking(self):
        self.assertFalse(self.engine.candidate("Сколько стоит беседка?", user_id=self.user.user_id))
        self.assertFalse(self.engine.candidate("Сколько стоит аренда беседки?", user_id=self.user.user_id))
        self.assertFalse(self.engine.candidate("Какие условия аренды корпуса?", user_id=self.user.user_id))
        self.assertFalse(self.engine.candidate("Расскажите про беседки", user_id=self.user.user_id))
        self.assertTrue(self.engine.candidate("Есть свободная беседка 12 октября?", user_id=self.user.user_id))

    async def test_corpus_date_range(self):
        backend = FakeBookingBackend(corpus=True)
        engine = BookingEngine(settings(self.tmp), self.db, backend=backend)
        try:
            r = await engine.handle_text(self.user, "Хотим снять корпус 14–16 ноября 2030, 25 человек")
            self.assertTrue(r.handled)
            self.assertIn("ФИО", r.text)
            create = next(payload for action, payload in backend.calls if action == "create_hold")
            self.assertIn("2030-11-14", create["start_at"])
            # Human end date is inclusive; backend boundary is next day.
            self.assertIn("2030-11-17", create["end_at"])
        finally:
            await engine.close()

    async def test_corpus_end_date_can_be_given_in_followup_message(self):
        backend = FakeBookingBackend(corpus=True)
        engine = BookingEngine(settings(self.tmp), self.db, backend=backend)
        try:
            r1 = await engine.handle_text(self.user, "Хотим снять корпус 14 ноября 2030, 25 человек")
            self.assertIn("До какого числа", r1.text)
            self.assertFalse(any(a == "create_hold" for a, _ in backend.calls))

            r2 = await engine.handle_text(self.user, "16 ноября 2030")
            self.assertIn("ФИО", r2.text)
            create = next(payload for action, payload in backend.calls if action == "create_hold")
            self.assertIn("2030-11-14", create["start_at"])
            self.assertIn("2030-11-17", create["end_at"])
        finally:
            await engine.close()

    async def test_editing_party_after_hold_releases_and_rechecks_slot(self):
        r1 = await self.engine.handle_text(
            self.user, "Хочу беседку 12 октября 2030 в 14:00 на 2 часа, 10 человек"
        )
        self.assertIn("ФИО", r1.text)
        self.assertEqual(sum(a == "create_hold" for a, _ in self.backend.calls), 1)

        r2 = await self.engine.handle_text(self.user, "Нас будет 20 человек")
        self.assertIn("ФИО", r2.text)
        self.assertTrue(any(a == "cancel_booking" for a, _ in self.backend.calls))
        holds = [payload for action, payload in self.backend.calls if action == "create_hold"]
        self.assertEqual(len(holds), 2)
        self.assertEqual(holds[-1]["guest_count"], 20)


class FirstContactBookingAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_parent_gets_intro_then_booking_reply_without_repeating_question(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            db = Database(tmp / "db.sqlite3")
            backend = FakeBookingBackend()
            engine = BookingEngine(settings(tmp), db, backend=backend)
            operator = AsyncMock()
            operator.escalate = AsyncMock(return_value=777)
            adapter = VKAdapter(settings(tmp), object(), db, operator, booking=engine)
            user = VKUser(123456, 123456, "Тест Родитель", "test_parent")
            adapter._user = AsyncMock(return_value=user)
            adapter.send_message = AsyncMock(return_value=1)
            adapter._manager_command = AsyncMock(return_value=False)
            try:
                await adapter._handle_message_new({"object": {"message": {
                    "from_id": user.user_id,
                    "peer_id": user.peer_id,
                    "text": "Добрый вечер, хочу забронировать беседку 12 октября 2030 в 14:00 на 2 часа, 10 человек",
                }}})
                self.assertEqual(adapter.send_message.await_count, 2)
                intro = adapter.send_message.await_args_list[0].args[1]
                booking_reply = adapter.send_message.await_args_list[1].args[1]
                self.assertIn("бот технической поддержки", intro)
                self.assertIn("ФИО", booking_reply)
                self.assertTrue(any(a == "create_hold" for a, _ in backend.calls))
            finally:
                await adapter.close()


class BookingReleaseTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(VERSION, "6.0.4")

    def test_booking_env_is_opt_in(self):
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("BOOKINGS_ENABLED=false", text)
        self.assertIn("BOOKING_API_URL", text)
        self.assertIn("BOOKING_API_SECRET", text)

    def test_apps_script_has_lock_and_secret(self):
        text = (ROOT / "google_apps_script" / "Code.gs").read_text(encoding="utf-8")
        self.assertIn("LockService.getScriptLock", text)
        self.assertIn("PropertiesService.getScriptProperties", text)
        self.assertIn("function doPost", text)
        self.assertIn("pending_manager", text)

    def test_apps_script_starts_services_disabled(self):
        text = (ROOT / "google_apps_script" / "Code.gs").read_text(encoding="utf-8")
        self.assertIn("[false,'gazebo'", text)
        self.assertIn("[false,'corpus'", text)

    def test_booking_keyboards(self):
        self.assertIn("Подтвердить заявку", parent_booking_confirmation_keyboard())
        self.assertIn("booking_manager", manager_booking_keyboard("ZV-ABC"))

    def test_booking_session_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "db.sqlite3")
            db.save_booking_session(
                user_id=1, peer_id=1, step="phone", status="active",
                payload={"service_key": "gazebo", "booking_id": "ZV-1"},
                external_booking_id="ZV-1", ttl_minutes=60,
            )
            row = db.get_booking_session(user_id=1)
            self.assertEqual(row["step"], "phone")
            self.assertEqual(row["payload"]["booking_id"], "ZV-1")
            self.assertEqual(db.find_booking_session_by_external_id("ZV-1")["user_id"], 1)

    def test_seen_user_check(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "db.sqlite3")
            self.assertFalse(db.has_seen_user(user_id=9))
            db.track(user_id=9, peer_id=9, kind="message", name="text")
            self.assertTrue(db.has_seen_user(user_id=9))

    def test_booking_connection_script_starts_from_project_root(self):
        env = os.environ.copy()
        env.pop("BOOKING_API_URL", None)
        env.pop("BOOKING_API_SECRET", None)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "test_booking_connection.py")],
            cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("BOOKING_API_URL or BOOKING_API_SECRET is missing", proc.stdout)


if __name__ == "__main__":
    unittest.main()
