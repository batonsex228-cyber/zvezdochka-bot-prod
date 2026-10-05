from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.booking import BookingEngine, BookingUser
from app.config import Settings
from app.conversation import classify_social_only, social_reply
from app.db import Database
from app.version import VERSION
from app.vk_adapter import VKAdapter, VKUser

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
        booking_timeout_seconds=15.0,
    )
    base.update(kw)
    return Settings(**base)


class FakeBookingBackend:
    def __init__(self, delay: float = 0.0):
        self.calls: list[tuple[str, dict]] = []
        self.delay = delay

    async def call(self, action: str, payload=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        payload = dict(payload or {})
        self.calls.append((action, payload))
        if action == "get_service":
            return {
                "found": True,
                "enabled": True,
                "service_key": "gazebo",
                "service_name": "Беседка",
                "resource_key": "",
                "resource_name": "",
                "mode": "hourly",
                "open_time": "08:00",
                "close_time": "23:00",
                "min_duration_minutes": 180,
                "max_duration_minutes": 900,
                "max_guests": 20,
                "manager_approval": True,
            }
        if action == "create_hold":
            return {
                "available": True,
                "booking_id": "ZV-V602",
                "expires_at": "2030-10-18T14:15:00+04:00",
                "resource_key": "gazebo-1",
                "resource_name": "Беседка №1",
            }
        if action == "cancel_booking":
            return {"cancelled": True}
        if action == "submit_booking":
            return {"submitted": True, "booking_id": "ZV-V602"}
        if action == "health":
            return {"healthy": True}
        raise AssertionError(action)

    async def close(self):
        return None


class SocialClassifierTests(unittest.TestCase):
    def test_release_version(self):
        self.assertEqual(VERSION, "6.0.3")

    def test_thanks_variants(self):
        for text in ["Спасибо", "Спасибо большое!", "Понятно, спасибо 😊", "Благодарю Вас"]:
            with self.subTest(text=text):
                self.assertEqual(classify_social_only(text), "thanks")

    def test_farewell_variants(self):
        for text in ["До свидания", "Всего доброго!", "Спасибо, до встречи", "Хорошего дня"]:
            with self.subTest(text=text):
                self.assertEqual(classify_social_only(text), "farewell")

    def test_ack_ping_and_reaction(self):
        self.assertEqual(classify_social_only("Окей"), "acknowledgement")
        self.assertEqual(classify_social_only("Вы тут?"), "ping")
        self.assertEqual(classify_social_only("👍💛"), "reaction")
        self.assertEqual(classify_social_only("???"), "ping")

    def test_real_question_is_never_swallowed_by_thanks(self):
        self.assertIsNone(classify_social_only("Спасибо, а какие документы нужны?"))
        self.assertIsNone(classify_social_only("Спасибо, а сколько стоит путёвка?"))
        self.assertIsNone(classify_social_only("Не помогло"))

    def test_replies_are_human_and_non_escalating(self):
        self.assertIn("пожалуйста", social_reply("thanks").lower())
        self.assertIn("до скорой встречи", social_reply("farewell").lower())
        self.assertIn("я здесь", social_reply("ping").lower())


class SocialAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.db = Database(self.tmp / "db.sqlite3")
        self.s = settings(self.tmp)
        self.operator = AsyncMock()
        self.operator.escalate = AsyncMock(return_value=777)
        self.adapter = VKAdapter(self.s, AsyncMock(), self.db, self.operator)
        self.user = VKUser(123456, 123456, "Тест Родитель", "parent")
        self.adapter._user = AsyncMock(return_value=self.user)
        self.adapter._manager_command = AsyncMock(return_value=False)
        self.adapter.send_message = AsyncMock(return_value=1)
        self.adapter.api = AsyncMock(return_value=1)
        # Avoid first-contact welcome noise in these tests.
        self.db.track(user_id=self.user.user_id, peer_id=self.user.peer_id, kind="message", name="seed")

    async def asyncTearDown(self):
        await self.adapter.close()
        self.td.cleanup()

    async def test_polite_closures_never_create_manager_ticket(self):
        for text in ["Спасибо!", "До свидания", "Понятно, спасибо", "👍"]:
            with self.subTest(text=text):
                self.adapter.send_message.reset_mock()
                self.operator.escalate.reset_mock()
                await self.adapter._handle_message_new({"object": {"message": {
                    "from_id": self.user.user_id,
                    "peer_id": self.user.peer_id,
                    "text": text,
                }}})
                self.operator.escalate.assert_not_awaited()
                self.assertEqual(self.adapter.send_message.await_count, 1)
                with self.db._conn() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 0)

    async def test_thanks_with_a_real_question_reaches_normal_processing(self):
        self.adapter.core.process = AsyncMock()
        # We only need to prove this is not intercepted as smalltalk.  Let the regular
        # path raise a sentinel before it could create any support ticket.
        self.adapter.core.process.side_effect = RuntimeError("NORMAL_ROUTER_REACHED")
        with self.assertRaisesRegex(RuntimeError, "NORMAL_ROUTER_REACHED"):
            await self.adapter._handle_message_new({"object": {"message": {
                "from_id": self.user.user_id,
                "peer_id": self.user.peer_id,
                "text": "Спасибо, а какие документы нужны?",
            }}})


class SocialDuringBookingTests(unittest.IsolatedAsyncioTestCase):
    async def test_thanks_does_not_get_consumed_as_booking_field_or_cancel_session(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            db = Database(tmp / "db.sqlite3")
            backend = FakeBookingBackend()
            engine = BookingEngine(settings(tmp), db, backend=backend)
            operator = AsyncMock()
            operator.escalate = AsyncMock(return_value=777)
            adapter = VKAdapter(settings(tmp), AsyncMock(), db, operator, booking=engine)
            user = VKUser(123456, 123456, "Тест Родитель", "parent")
            adapter._user = AsyncMock(return_value=user)
            adapter._manager_command = AsyncMock(return_value=False)
            adapter.send_message = AsyncMock(return_value=1)
            adapter.api = AsyncMock(return_value=1)
            db.track(user_id=user.user_id, peer_id=user.peer_id, kind="message", name="seed")
            try:
                await engine.handle_text(
                    BookingUser(user.user_id, user.peer_id, user.full_name, user.username),
                    "Хочу забронировать беседку",
                )
                before = db.get_booking_session(user_id=user.user_id)
                self.assertEqual(before["step"], "date")
                calls_before = len(backend.calls)

                await adapter._handle_message_new({"object": {"message": {
                    "from_id": user.user_id, "peer_id": user.peer_id, "text": "Спасибо!"
                }}})

                after = db.get_booking_session(user_id=user.user_id)
                self.assertIsNotNone(after)
                self.assertEqual(after["step"], "date")
                self.assertEqual(len(backend.calls), calls_before)
                operator.escalate.assert_not_awaited()
            finally:
                await adapter.close()


class TypingKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_typing_pulses_for_entire_slow_message_handler_and_stops(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            db = Database(tmp / "db.sqlite3")
            adapter = VKAdapter(settings(tmp), AsyncMock(), db, AsyncMock())
            adapter.TYPING_REFRESH_SECONDS = 0.01
            adapter.typing = AsyncMock(return_value=None)

            async def slow_handler(update):
                await asyncio.sleep(0.045)

            adapter._handle_message_new = slow_handler
            try:
                await adapter._handle_update({
                    "type": "message_new",
                    "object": {"message": {"from_id": 7, "peer_id": 7, "text": "бронь"}},
                })
                self.assertGreaterEqual(adapter.typing.await_count, 3)
                count_after = adapter.typing.await_count
                await asyncio.sleep(0.03)
                self.assertEqual(adapter.typing.await_count, count_after)
            finally:
                await adapter.close()

    async def test_callback_updates_get_typing_too(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            db = Database(tmp / "db.sqlite3")
            adapter = VKAdapter(settings(tmp), AsyncMock(), db, AsyncMock())
            adapter.TYPING_REFRESH_SECONDS = 0.01
            adapter.typing = AsyncMock(return_value=None)

            async def slow_event(update):
                await asyncio.sleep(0.025)

            adapter._handle_message_event = slow_event
            try:
                await adapter._handle_update({
                    "type": "message_event",
                    "object": {"user_id": 7, "peer_id": 7, "event_id": "e", "payload": {}},
                })
                self.assertGreaterEqual(adapter.typing.await_count, 2)
            finally:
                await adapter.close()


class BookingTypingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_booking_handler_keeps_typing_alive_while_backend_is_slow(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            db = Database(tmp / "db.sqlite3")
            backend = FakeBookingBackend(delay=0.025)
            engine = BookingEngine(settings(tmp), db, backend=backend)
            operator = AsyncMock()
            operator.escalate = AsyncMock(return_value=777)
            adapter = VKAdapter(settings(tmp), AsyncMock(), db, operator, booking=engine)
            user = VKUser(123456, 123456, "Тест Родитель", "parent")
            adapter._user = AsyncMock(return_value=user)
            adapter._manager_command = AsyncMock(return_value=False)
            adapter.send_message = AsyncMock(return_value=1)
            adapter.api = AsyncMock(return_value=1)
            adapter.typing = AsyncMock(return_value=None)
            adapter.TYPING_REFRESH_SECONDS = 0.01
            db.track(user_id=user.user_id, peer_id=user.peer_id, kind="message", name="seed")
            try:
                await adapter._handle_update({
                    "type": "message_new",
                    "object": {"message": {
                        "from_id": user.user_id,
                        "peer_id": user.peer_id,
                        "text": "Беседка 18 октября 2030 с 14 до 17, 10 человек",
                    }},
                })
                self.assertGreaterEqual(adapter.typing.await_count, 3)
                sent = "\n".join(call.args[1] for call in adapter.send_message.await_args_list)
                self.assertIn("шаг 3 из 3", sent)
                self.assertIn("ФИО", sent)
                operator.escalate.assert_not_awaited()
            finally:
                await adapter.close()


class BookingCopyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.db = Database(self.tmp / "db.sqlite3")
        self.backend = FakeBookingBackend()
        self.engine = BookingEngine(settings(self.tmp), self.db, backend=self.backend)
        self.user = BookingUser(123456, 123456, "Тест Родитель", "parent")

    async def asyncTearDown(self):
        await self.engine.close()
        self.td.cleanup()

    async def test_gazebo_flow_has_three_clear_friendly_stages(self):
        r1 = await self.engine.handle_text(self.user, "Хочу забронировать беседку")
        self.assertIn("шаг 1 из 3", r1.text)
        self.assertIn("На какой день", r1.text)

        r2 = await self.engine.handle_text(self.user, "18 октября 2030")
        self.assertIn("шаг 1 из 3", r2.text)
        self.assertIn("во сколько", r2.text)

        r3 = await self.engine.handle_text(self.user, "14:00")
        self.assertIn("шаг 1 из 3", r3.text)
        self.assertIn("на сколько часов", r3.text.lower())
        self.assertIn("3 часа", r3.text)

        r4 = await self.engine.handle_text(self.user, "3 часа")
        self.assertIn("шаг 2 из 3", r4.text)
        self.assertIn("Сколько примерно будет человек", r4.text)
        self.assertIn("до 20 человек", r4.text)

        r5 = await self.engine.handle_text(self.user, "10")
        self.assertIn("шаг 3 из 3", r5.text)
        self.assertIn("ФИО", r5.text)

        r6 = await self.engine.handle_text(self.user, "Иванов Иван Иванович")
        self.assertIn("шаг 3 из 3", r6.text)
        self.assertIn("последний вопрос", r6.text.lower())
        self.assertIn("номер телефона", r6.text)

        r7 = await self.engine.handle_text(self.user, "+7 912 345-67-89")
        self.assertEqual(r7.state, "confirm_parent")
        self.assertIn("Проверьте заявку, пожалуйста", r7.text)
        self.assertIn("18 октября 2030", r7.text)
        self.assertIn("14:00–17:00", r7.text)

    async def test_initial_message_can_skip_ahead_without_breaking_progress(self):
        r = await self.engine.handle_text(
            self.user,
            "Беседка 18 октября 2030 с 14 до 17, 10 человек",
        )
        self.assertIn("шаг 3 из 3", r.text)
        self.assertIn("ФИО", r.text)
        self.assertTrue(any(action == "create_hold" for action, _ in self.backend.calls))


if __name__ == "__main__":
    unittest.main()
