from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.booking import BookingEngine, BookingUser, is_booking_request, public_rental_info
from app.config import Settings
from app.db import Database
from app.intake import IntakeUser, SmartHandoffEngine, public_event_info, classify_transaction
from app.presentation import present_answer
from app.responder import BotAnswer
from app.version import VERSION
from app.vk_adapter import VKAdapter, VKUser


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
        smart_handoff_enabled=False,
        smart_handoff_ttl_minutes=90,
    )
    base.update(kw)
    return Settings(**base)


class FakeBackend:
    def __init__(self, service_key="gazebo"):
        self.calls = []
        self.service_key = service_key

    async def call(self, action, payload=None):
        payload = dict(payload or {})
        self.calls.append((action, payload))
        if action == "get_service":
            if payload.get("service_key") == "corpus":
                return {
                    "found": True, "enabled": True, "service_key": "corpus", "service_name": "Тур выходного дня",
                    "resource_key": "corpus-1", "resource_name": "Корпус №1", "mode": "date_range",
                    "open_time": "", "close_time": "", "min_duration_minutes": 0, "max_duration_minutes": 0,
                    "max_guests": 100, "manager_approval": True, "price_mode": "per_person",
                    "price_amount": 1450, "price_unit": "с человека", "price_duration_minutes": 0,
                    "price_includes": "Проживание, ужин и завтрак",
                }
            return {
                "found": True, "enabled": True, "service_key": "gazebo", "service_name": "Беседка",
                "resource_key": "", "resource_name": "", "mode": "hourly",
                "open_time": "08:00", "close_time": "23:00", "min_duration_minutes": 180,
                "max_duration_minutes": 900, "max_guests": 20, "manager_approval": True,
                "price_mode": "fixed_duration", "price_amount": 3300, "price_unit": "за 3 часа",
                "price_duration_minutes": 180, "price_includes": "Мангал, уголь, розжиг и решётка",
            }
        if action == "create_hold":
            return {"available": True, "booking_id": "ZV-603", "expires_at": "2030-10-18T18:00:00+04:00",
                    "resource_key": "corpus-1" if payload.get("service_key") == "corpus" else "gazebo-1",
                    "resource_name": "Корпус №1" if payload.get("service_key") == "corpus" else "Беседка №1"}
        if action == "submit_booking":
            return {"submitted": True, "booking_id": "ZV-603"}
        if action == "cancel_booking":
            return {"cancelled": True}
        if action == "health":
            return {"healthy": True, "version": "5.9.2"}
        raise AssertionError(action)

    async def close(self):
        return None


class RentalCatalogTests(unittest.TestCase):
    def test_release_version(self):
        self.assertEqual(VERSION, "6.0.4")

    def test_gazebo_public_price(self):
        text = public_rental_info("Добрый вечер, сколько стоит аренда беседки и что входит?")
        self.assertIn("3 300 ₽", text)
        self.assertIn("мангал", text.lower())
        self.assertIn("уголь", text.lower())

    def test_natural_price_wording_does_not_start_booking(self):
        samples = [
            "Сколько будет стоить аренда беседки?",
            "Во сколько обойдётся беседка?",
            "Сколько нужно заплатить за беседку?",
            "Сколько аренда беседки?",
        ]
        for question in samples:
            with self.subTest(question=question):
                text = public_rental_info(question)
                self.assertIsNotNone(text)
                self.assertIn("3 300 ₽", text)
                self.assertFalse(is_booking_request(question))

    def test_corpus_public_price(self):
        text = public_rental_info("Сколько стоит снять корпус с ночёвкой?")
        self.assertIn("1 450 ₽", text)
        self.assertIn("ужин", text.lower())
        self.assertIn("завтрак", text.lower())

    def test_named_weekend_tour_presence_question_is_answered(self):
        for question in ["Есть ли тур выходного дня?", "У вас есть тур выходного дня?", "Что такое тур выходного дня?"]:
            with self.subTest(question=question):
                text = public_rental_info(question)
                self.assertIsNotNone(text)
                self.assertIn("1 450 ₽", text)

    def test_plain_presence_questions_are_answered_but_dated_availability_is_not(self):
        for question, price in [("Есть ли у вас беседки?", "3 300 ₽"), ("У вас есть проживание?", "1 450 ₽")]:
            with self.subTest(question=question):
                text = public_rental_info(question)
                self.assertIsNotNone(text)
                self.assertIn(price, text)
        self.assertIsNone(public_rental_info("Есть ли у вас беседка на 18 октября?"))
        self.assertTrue(is_booking_request("Есть ли у вас беседка на 18 октября?"))

    def test_corpus_and_weekend_tour_are_booking_intents(self):
        self.assertTrue(is_booking_request("Хочу снять корпус на выходные"))
        self.assertTrue(is_booking_request("Хочу забронировать тур выходного дня"))
        self.assertTrue(is_booking_request("Нужно проживание в лагере с ночёвкой"))

    def test_terse_weekend_tour_desire_is_booking_intent(self):
        for question in ["Хочу тур выходного дня", "Хотим на выходные в корпус"]:
            with self.subTest(question=question):
                self.assertTrue(is_booking_request(question))
                self.assertEqual(classify_transaction(question), "corpus_request")

    def test_event_price_is_not_invented(self):
        text = public_event_info("Сколько стоит провести корпоратив в банкетном зале?")
        self.assertIn("рассчитывается индивидуально", text.lower())
        self.assertIn("120", text)
        self.assertIn("200", text)

    def test_gazebo_price_wins_even_when_event_purpose_is_mentioned(self):
        question = "Сколько стоит беседка на день рождения?"
        self.assertIsNone(public_event_info(question))
        text = public_rental_info(question)
        self.assertIsNotNone(text)
        self.assertIn("3 300 ₽", text)





    def test_natural_guest_count_wording_is_accepted(self):
        from app.booking import _parse_guest_count
        from app.intake import _extract_guest_count, _has_stay_date_hint

        self.assertEqual(_parse_guest_count("нас 10"), 10)
        self.assertEqual(_parse_guest_count("нас будет примерно 12"), 12)
        self.assertEqual(_extract_guest_count("18–19 октября, нас 12"), 12)
        self.assertEqual(_extract_guest_count("18–19 октбря, нас будет 12"), 12)
        self.assertTrue(_has_stay_date_hint("18–19 октбря, нас будет 12"))

class RoutingRegressionTests(unittest.TestCase):
    def test_child_corpus_questions_are_not_misread_as_rental(self):
        samples = [
            "Хочу узнать, в каком корпусе будет жить мой ребёнок",
            "Хочу связаться с ребёнком в корпусе",
            "Хочу узнать про проживание детей в лагере",
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertFalse(is_booking_request(text))
                self.assertNotEqual(classify_transaction(text), "corpus_request")

    def test_informational_event_questions_do_not_start_form(self):
        samples = [
            "Хочу узнать, какие соревнования проходят в лагере",
            "Хочу узнать про банкетный зал",
            "У вас есть банкетный зал?",
        ]
        self.assertIsNone(classify_transaction(samples[0]))
        self.assertIsNone(classify_transaction(samples[1]))
        self.assertIsNone(public_event_info(samples[0]))
        self.assertIsNotNone(public_event_info(samples[1]))
        self.assertIsNotNone(public_event_info(samples[2]))
        for text in [
            "Хочу узнать, какие мероприятия проходят у детей",
            "Ребёнок сказал, что сегодня мероприятие",
            "Какие турниры проходят в лагере?",
            "Где спортивная площадка?",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(public_event_info(text))
                self.assertNotEqual(classify_transaction(text), "event_request")

    def test_actionable_event_still_starts_form(self):
        self.assertEqual(classify_transaction("Хочу провести корпоратив в лагере"), "event_request")
        self.assertEqual(classify_transaction("Нужен зал на выпускной"), "event_request")
        self.assertEqual(classify_transaction("Нужен банкетный зал на выпускной"), "event_request")
        self.assertEqual(classify_transaction("Нужен конференц-зал"), "event_request")
        self.assertEqual(classify_transaction("Хочу арендовать банкетный зал"), "event_request")
        self.assertEqual(classify_transaction("Планируем корпоратив в лагере"), "event_request")
        self.assertEqual(classify_transaction("Хочу снять зал на выпускной"), "event_request")

    def test_explicit_gazebo_or_corpus_rental_is_not_hijacked_by_event_router(self):
        self.assertNotEqual(classify_transaction("Хочу арендовать беседку для дня рождения"), "event_request")
        self.assertEqual(classify_transaction("Хочу арендовать корпус для корпоратива"), "corpus_request")
        self.assertTrue(is_booking_request("Хочу арендовать беседку для дня рождения"))
        self.assertTrue(is_booking_request("Хочу арендовать корпус для корпоратива"))

    def test_possibility_event_question_stays_in_public_info(self):
        for text in ["Можно провести корпоратив?", "Проводите свадьбы?", "Есть ли конференц-зал?"]:
            with self.subTest(text=text):
                self.assertIsNotNone(public_event_info(text))
                self.assertNotEqual(classify_transaction(text), "event_request")

    def test_extra_services_presentation_has_current_official_prices(self):
        answer = BotAnswer(True, "", faq_id="extra-services", confidence=1.0, reason="test", source="https://zvezdaglazov.ru/pages/extra-services.html")
        text = present_answer("Какие есть дополнительные услуги?", answer).text
        self.assertIn("3 300 ₽", text)
        self.assertIn("1 450 ₽", text)
        self.assertGreaterEqual(text.count("500 ₽"), 2)
        self.assertNotIn("2 700 ₽", text)
        self.assertNotIn("800 ₽", text)
        self.assertNotIn("400 ₽", text)

class CurrentPublicDataRegressionTests(unittest.TestCase):
    def test_current_2027_shift_prices_are_not_stale(self):
        root = Path(__file__).resolve().parents[1]
        seed = json.loads((root / "data" / "seed_faq.json").read_text(encoding="utf-8"))
        items = {item["id"]: item for item in seed["faq"]}
        for faq_id in ["summer-shifts-2027", "all-prices", "shift-1-2027", "shift-2-2027", "shift-3-2027", "shift-4-2027"]:
            self.assertIn(faq_id, items)
            blob = json.dumps(items[faq_id], ensure_ascii=False)
            self.assertIn("40 000", blob)
            self.assertNotIn("34 000", blob)
        self.assertNotIn("summer-shifts-2026", items)
        for old_id in ["shift-1-2026", "shift-2-2026", "shift-3-2026", "shift-4-2026"]:
            self.assertNotIn(old_id, items)


class BookingPricingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.db = Database(self.tmp / "db.sqlite3")
        self.backend = FakeBackend()
        self.engine = BookingEngine(settings(self.tmp), self.db, backend=self.backend)
        self.user = BookingUser(101, 101, "Иванов Иван Иванович", "ivanov")

    async def asyncTearDown(self):
        await self.engine.close()
        self.td.cleanup()

    async def _gazebo_until_confirm(self, duration="3 часа"):
        await self.engine.handle_text(self.user, "Хочу забронировать беседку")
        await self.engine.handle_text(self.user, "18 октября 2030")
        await self.engine.handle_text(self.user, "14:00")
        await self.engine.handle_text(self.user, duration)
        await self.engine.handle_text(self.user, "10")
        await self.engine.handle_text(self.user, "Иванов Иван Иванович")
        return await self.engine.handle_text(self.user, "+7 912 345-67-89")

    async def test_standard_gazebo_confirmation_has_exact_total(self):
        result = await self._gazebo_until_confirm()
        self.assertEqual(result.state, "confirm_parent")
        self.assertIn("К ОПЛАТЕ: 3 300 ₽", result.text)
        await self.engine.parent_decision(self.user, confirm=True)
        submit = [p for a,p in self.backend.calls if a == "submit_booking"][-1]
        self.assertEqual(submit["price_amount"], 3300)
        self.assertIn("3 300 ₽", submit["price_text"])

    async def test_nonstandard_gazebo_duration_does_not_invent_total(self):
        result = await self._gazebo_until_confirm("4 часа")
        self.assertIn("3 300 ₽ за 3 часа", result.text)
        self.assertIn("итоговую сумму подтвердит менеджер", result.text.lower())

    async def test_weekend_tour_price_is_per_person_total(self):
        result = await self.engine.handle_text(
            self.user,
            "Хочу забронировать корпус с 18 по 19 октября 2030 на 10 человек",
        )
        # Range wording is intentionally collected over the deterministic form if not parsed in one shot.
        if "ФИО" not in result.text:
            if "До какого числа" in result.text:
                result = await self.engine.handle_text(self.user, "19 октября 2030")
            if "Сколько примерно" in result.text:
                result = await self.engine.handle_text(self.user, "10")
        self.assertIn("ФИО", result.text)
        result = await self.engine.handle_text(self.user, "Иванов Иван Иванович")
        result = await self.engine.handle_text(self.user, "+7 912 345-67-89")
        self.assertEqual(result.state, "confirm_parent")
        self.assertIn("14 500 ₽", result.text)
        self.assertIn("1 450 ₽ × 10 чел.", result.text)

    async def test_common_october_typo_is_parsed(self):
        result = await self.engine.handle_text(self.user, "Хочу забронировать беседку на 7 октбря 2030")
        self.assertIn("7 октября 2030", result.text)
        self.assertIn("во сколько", result.text.lower())

    async def test_configured_sheet_price_wins_over_bundled_fallback(self):
        amount, text = self.engine._price_details(
            {"service_key": "gazebo", "duration_minutes": 180, "guest_count": 10},
            {"service_key": "gazebo", "price_amount": 3500, "price_duration_minutes": 180, "price_unit": "за 3 часа"},
        )
        self.assertEqual(amount, 3500)
        self.assertIn("3 500 ₽", text)

    async def test_active_booking_does_not_swallow_event_switch(self):
        await self.engine.handle_text(self.user, "Хочу забронировать беседку")
        self.assertFalse(self.engine.candidate("Хочу провести корпоратив в лагере", user_id=self.user.user_id))



class EventRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.tmp = Path(self.td.name)
        self.db = Database(self.tmp / "db.sqlite3")
        self.engine = SmartHandoffEngine(settings(self.tmp, smart_handoff_enabled=False), self.db)
        self.user = IntakeUser(202, 202, "Петров Пётр", "petrov")

    async def asyncTearDown(self):
        self.td.cleanup()

    async def test_event_request_works_even_when_general_smart_handoff_off(self):
        self.assertTrue(self.engine.candidate("Хочу организовать спортивные соревнования в лагере", user_id=self.user.user_id))
        first = await self.engine.handle_text(self.user, "Хочу организовать спортивные соревнования в лагере")
        self.assertIn("что хотите организовать", first.text.lower())
        second = await self.engine.handle_text(self.user, "Спортивные соревнования 20 октября, 50 человек")
        self.assertIn("ФИО", second.text)
        third = await self.engine.handle_text(self.user, "Петров Пётр Петрович")
        self.assertIn("номер телефона", third.text.lower())
        fourth = await self.engine.handle_text(self.user, "+7 912 111-22-33")
        self.assertTrue(fourth.needs_parent_confirmation)
        self.assertIn("Своё мероприятие", fourth.text)
        self.assertIn("спортивные соревнования", fourth.text.lower())

    async def test_regular_smart_handoff_stays_off(self):
        self.assertFalse(self.engine.candidate("Хочу вернуть деньги за путёвку", user_id=self.user.user_id))

    async def test_generic_event_request_still_asks_for_planning_details(self):
        first = await self.engine.handle_text(self.user, "Хочу провести мероприятие у вас")
        self.assertIn("что хотите организовать", first.text.lower())
        self.assertIn("сколько будет человек", first.text.lower())

    async def test_corpus_request_with_only_date_still_asks_for_guests(self):
        first = await self.engine.handle_text(self.user, "Хочу снять корпус 18 октября")
        self.assertIn("сколько будет человек", first.text.lower())
        self.assertNotIn("фио", first.text.lower())


    async def test_corpus_fallback_requires_guest_count_and_shows_total(self):
        first = await self.engine.handle_text(self.user, "Хочу снять корпус с ночёвкой")
        self.assertIn("1 450 ₽", first.text)
        missing_count = await self.engine.handle_text(self.user, "18–19 октября")
        self.assertIn("количество гостей", missing_count.text.lower())
        details = await self.engine.handle_text(self.user, "18–19 октября, 12 человек")
        self.assertIn("ФИО", details.text)
        await self.engine.handle_text(self.user, "Петров Пётр Петрович")
        summary = await self.engine.handle_text(self.user, "+7 912 111-22-33")
        self.assertTrue(summary.needs_parent_confirmation)
        self.assertIn("17 400 ₽", summary.text)
        self.assertIn("1 450 ₽ × 12 чел.", summary.text)
        submitted = await self.engine.parent_decision(self.user, confirm=True)
        self.assertIsNotNone(submitted.submission)
        display = {item["label"]: item["value"] for item in submitted.submission["display_fields"]}
        self.assertIn("17 400 ₽", display["Стоимость"])

    async def test_corpus_fallback_requires_date_or_explicit_unknown(self):
        await self.engine.handle_text(self.user, "Хочу снять корпус с ночёвкой")
        missing_date = await self.engine.handle_text(self.user, "12 человек")
        self.assertIn("желаемых дат", missing_date.text.lower())


class PriceAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_price_question_does_not_create_manager_ticket(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            db = Database(tmp / "db.sqlite3")
            operator = AsyncMock()
            operator.escalate = AsyncMock(return_value=777)
            adapter = VKAdapter(settings(tmp), AsyncMock(), db, operator)
            user = VKUser(303,303,"Тест Родитель","parent")
            adapter._user = AsyncMock(return_value=user)
            adapter._manager_command = AsyncMock(return_value=False)
            adapter.send_message = AsyncMock(return_value=1)
            adapter.api = AsyncMock(return_value=1)
            db.track(user_id=user.user_id, peer_id=user.peer_id, kind="message", name="seed")
            try:
                for question in [
                    "Сколько стоит аренда беседки?",
                    "Сколько будет стоить аренда беседки?",
                    "Во сколько обойдётся беседка?",
                ]:
                    with self.subTest(question=question):
                        operator.escalate.reset_mock()
                        await adapter._handle_message_new({"object":{"message":{"from_id":303,"peer_id":303,"text":question}}})
                        operator.escalate.assert_not_awaited()
                        sent = adapter.send_message.await_args.args[1]
                        self.assertIn("3 300 ₽", sent)
            finally:
                await adapter.close()


class CorpusSubmissionPriceAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_corpus_fallback_manager_summary_contains_calculated_price(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            db = Database(tmp / "db.sqlite3")
            operator = AsyncMock()
            operator.escalate_intake = AsyncMock(return_value=901)
            intake = SmartHandoffEngine(settings(tmp, smart_handoff_enabled=False), db)
            adapter = VKAdapter(settings(tmp), AsyncMock(), db, operator, booking=None, intake=intake)
            user = VKUser(606,606,"Тест Родитель","parent")
            adapter._user = AsyncMock(return_value=user)
            adapter._manager_command = AsyncMock(return_value=False)
            adapter.send_message = AsyncMock(return_value=1)
            adapter.api = AsyncMock(return_value=1)
            db.track(user_id=user.user_id, peer_id=user.peer_id, kind="message", name="seed")
            try:
                for message in [
                    "Хочу снять корпус с ночёвкой",
                    "18–19 октября, 12 человек",
                    "Иванов Иван Иванович",
                    "+7 912 345-67-89",
                ]:
                    await adapter._handle_message_new({"object":{"message":{"from_id":606,"peer_id":606,"text":message}}})
                result = await intake.parent_decision(IntakeUser(606,606,"Тест Родитель","parent"), confirm=True)
                self.assertTrue(result.submission)
                await adapter._send_intake_result(user, result)
                summary = operator.escalate_intake.await_args.kwargs["summary_text"]
                self.assertIn("Стоимость", summary)
                self.assertIn("17 400 ₽", summary)
            finally:
                await adapter.close()


class CorpusFallbackAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_corpus_request_becomes_short_manager_form_when_booking_is_off(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            db = Database(tmp / "db.sqlite3")
            operator = AsyncMock()
            operator.escalate = AsyncMock(return_value=777)
            intake = SmartHandoffEngine(settings(tmp, smart_handoff_enabled=False), db)
            adapter = VKAdapter(settings(tmp), AsyncMock(), db, operator, booking=None, intake=intake)
            user = VKUser(404,404,"Тест Родитель","parent")
            adapter._user = AsyncMock(return_value=user)
            adapter._manager_command = AsyncMock(return_value=False)
            adapter.send_message = AsyncMock(return_value=1)
            adapter.api = AsyncMock(return_value=1)
            db.track(user_id=user.user_id, peer_id=user.peer_id, kind="message", name="seed")
            try:
                await adapter._handle_message_new({"object":{"message":{"from_id":404,"peer_id":404,"text":"Хочу снять корпус с ночёвкой"}}})
                operator.escalate.assert_not_awaited()
                sent = adapter.send_message.await_args.args[1]
                self.assertIn("1 450 ₽", sent)
                self.assertIn("желаемые даты", sent.lower())
                row = db.get_intake_session(user_id=404)
                self.assertIsNotNone(row)
                self.assertEqual(row["intake_type"], "corpus_request")
            finally:
                await adapter.close()


class CorpusDisabledServiceAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_corpus_calendar_falls_back_to_structured_form(self):
        class DisabledCorpusBackend(FakeBackend):
            async def call(self, action, payload=None):
                payload = dict(payload or {})
                self.calls.append((action, payload))
                if action == "get_service" and payload.get("service_key") == "corpus":
                    return {"found": False, "enabled": False, "service_key": "corpus"}
                return await super().call(action, payload)

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            db = Database(tmp / "db.sqlite3")
            backend = DisabledCorpusBackend()
            booking = BookingEngine(settings(tmp), db, backend=backend)
            intake = SmartHandoffEngine(settings(tmp, smart_handoff_enabled=False), db)
            operator = AsyncMock()
            operator.escalate = AsyncMock(return_value=777)
            adapter = VKAdapter(settings(tmp), AsyncMock(), db, operator, booking=booking, intake=intake)
            user = VKUser(505,505,"Тест Родитель","parent")
            adapter._user = AsyncMock(return_value=user)
            adapter._manager_command = AsyncMock(return_value=False)
            adapter.send_message = AsyncMock(return_value=1)
            adapter.api = AsyncMock(return_value=1)
            db.track(user_id=user.user_id, peer_id=user.peer_id, kind="message", name="seed")
            try:
                await adapter._handle_message_new({"object":{"message":{"from_id":505,"peer_id":505,"text":"Хочу снять корпус с ночёвкой"}}})
                operator.escalate.assert_not_awaited()
                sent = adapter.send_message.await_args.args[1]
                self.assertIn("1 450 ₽", sent)
                self.assertIn("желаемые даты", sent.lower())
                row = db.get_intake_session(user_id=505)
                self.assertIsNotNone(row)
                self.assertEqual(row["intake_type"], "corpus_request")
            finally:
                await adapter.close()


if __name__ == "__main__":
    unittest.main()
