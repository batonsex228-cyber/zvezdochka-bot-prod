from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import Settings, load_settings
from app.core import INTENT_FAQ, PINNED_UI_ANSWERS, SupportCore
from app.db import Database
from app.intent_router import route
from app.knowledge import KnowledgeBase
from app.operator import OperatorBridge
from app.responder import BotAnswer, StrictResponder, looks_dynamic_availability, looks_sensitive_or_individual
from app.version import VERSION
from app.vk_adapter import VKAPIError, VKAdapter, VKUser
from app.vk_keyboards import (
    admin_keyboard, answer_keyboard, clarification_keyboard, main_keyboard,
    manager_ticket_keyboard, notification_keyboard, newsletter_preview_keyboard,
    shifts_carousel_template,
)

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
    )
    base.update(kw)
    return Settings(**base)


def core_env():
    td = tempfile.TemporaryDirectory()
    tmp = Path(td.name)
    db = Database(tmp / "db.sqlite3")
    s = settings(tmp)
    kb = KnowledgeBase(tmp / "missing.json", ROOT / "data" / "seed_faq.json", db,
                       fallback_kb_file=ROOT / "data" / "knowledge_base.json", max_age_hours=1_000_000)
    responder = StrictResponder(s, kb)
    return td, tmp, db, s, kb, SupportCore(responder)


class VersionConfigTests(unittest.TestCase):
    def test_version(self): self.assertEqual(VERSION, "5.7.4")
    def test_no_aiogram_requirement(self): self.assertNotIn("aiogram", (ROOT / "requirements.txt").read_text())
    def test_no_telegram_secret_in_env_example(self): self.assertNotIn("TELEGRAM_BOT_TOKEN", (ROOT / ".env.example").read_text())
    def test_vk_required_in_env_example(self):
        t=(ROOT/".env.example").read_text(); self.assertIn("VK_GROUP_TOKEN",t); self.assertIn("VK_GROUP_ID",t)
    def test_content_token_optional_in_env_example(self): self.assertIn("VK_CONTENT_TOKEN", (ROOT/".env.example").read_text())
    def test_load_settings_requires_vk(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"VK_GROUP_TOKEN":"", "VK_GROUP_ID":"0"}, clear=False):
            with self.assertRaises(RuntimeError): load_settings()
    def test_settings_content_token_optional(self): self.assertIsNone(settings(Path(tempfile.mkdtemp())).vk_content_token)
    def test_legacy_site_url_is_migrated_to_root(self):
        env = {
            "VK_GROUP_TOKEN": "test-token",
            "VK_GROUP_ID": "241267977",
            "SITE_URL": "https://zvezdaglazov.ru/new/",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(load_settings().site_url, "https://zvezdaglazov.ru/")
    def test_source_defaults(self):
        s=settings(Path(tempfile.mkdtemp())); self.assertEqual(s.vk_source_post_limit,10); self.assertEqual(s.vk_source_max_age_days,60)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory(); self.db=Database(Path(self.td.name)/"x.sqlite3")
    def tearDown(self): self.td.cleanup()
    def test_ticket_flow(self):
        i=self.db.create_ticket(user_id=1,chat_id=1,username=None,full_name="A",question="Q"); self.assertEqual(self.db.get_ticket(i)["status"],"open")
    def test_ticket_answer(self):
        i=self.db.create_ticket(user_id=1,chat_id=1,username=None,full_name="A",question="Q"); self.db.save_human_answer(i,"A"); self.assertEqual(self.db.get_ticket(i)["status"],"answered")
    def test_open_tickets(self):
        self.db.create_ticket(user_id=1,chat_id=1,username=None,full_name="A",question="Q"); self.assertEqual(len(self.db.list_open_tickets()),1)
    def test_manual_faq(self): self.assertTrue(self.db.add_manual_faq("q","a")); self.assertFalse(self.db.add_manual_faq("q","a"))
    def test_answer_feedback(self):
        i=self.db.log_answer(user_id=1,chat_id=1,username=None,full_name="A",question="Q",faq_id="x",reason="r",confidence=1); self.db.set_feedback(i,True); self.assertEqual(self.db.get_answer_event(i)["helpful"],1)
    def test_feedback_ticket_link(self):
        e=self.db.log_answer(user_id=1,chat_id=1,username=None,full_name="A",question="Q",faq_id="x",reason="r",confidence=1); t=self.db.create_ticket(user_id=1,chat_id=1,username=None,full_name="A",question="Q"); self.db.link_feedback_ticket(e,t); self.assertEqual(self.db.get_answer_event(e)["escalated_ticket_id"],t)
    def test_track_analytics(self):
        self.db.track(user_id=1,peer_id=1,kind="message",name="text"); self.db.track(user_id=1,peer_id=1,kind="button",name="food"); s=self.db.analytics_stats(); self.assertEqual(s["users"],1); self.assertEqual(s["buttons"],1)
    def test_context_roundtrip(self):
        self.db.set_context(user_id=1,topic="shift",entity="shift:2",ttl_minutes=15); self.assertEqual(self.db.get_context(user_id=1)["entity"],"shift:2")
    def test_context_clear(self): self.db.set_context(user_id=1,topic="x",entity=None); self.db.clear_context(user_id=1); self.assertIsNone(self.db.get_context(user_id=1))
    def test_subscription_on_off(self): self.db.set_subscription(user_id=1,peer_id=1,enabled=True); self.assertTrue(self.db.is_subscribed(user_id=1)); self.db.set_subscription(user_id=1,peer_id=1,enabled=False); self.assertFalse(self.db.is_subscribed(user_id=1))
    def test_subscriber_ids(self): self.db.set_subscription(user_id=1,peer_id=11,enabled=True); self.db.set_subscription(user_id=2,peer_id=22,enabled=True); self.assertEqual(self.db.subscriber_peer_ids(),[11,22])
    def test_admin_state(self): self.db.set_admin_state(user_id=1,state="reply_ticket",payload={"ticket_id":2}); self.assertEqual(self.db.get_admin_state(user_id=1)[1]["ticket_id"],2)
    def test_admin_state_clear(self): self.db.set_admin_state(user_id=1,state="x"); self.db.set_admin_state(user_id=1,state=None); self.assertIsNone(self.db.get_admin_state(user_id=1))
    def test_campaign(self): i=self.db.create_campaign(text="hello",created_by=1,target_count=5); self.assertEqual(self.db.get_campaign(i)["status"],"draft"); self.db.finish_campaign(i,sent=4,failed=1); self.assertEqual(self.db.get_campaign(i)["sent_count"],4)
    def test_export_csv(self): self.assertTrue(self.db.export_feedback_csv().startswith(b"\xef\xbb\xbf"))
    def test_containment_rate(self):
        self.db.log_answer(user_id=1,chat_id=1,username=None,full_name="A",question="Q",faq_id="x",reason="r",confidence=1); self.db.create_ticket(user_id=2,chat_id=2,username=None,full_name="B",question="Q"); self.assertEqual(self.db.analytics_stats()["containment_rate"],50.0)


ROUTER_CASES = [
    ("📅 Смены и цены","shifts_prices"),("какие смены есть","shifts_prices"),("даты смен","shifts_prices"),("стоимость путевки","shifts_prices"),
    ("когда первая смена","shift_1"),("скок 1-я стоит","shift_1"),("первая когда начинается","shift_1"),
    ("когда вторая смена","shift_2"),("скок 2-я стоит","shift_2"),("а 2-я когда?","shift_2"),("цена второй смены","shift_2"),
    ("когда третья смена","shift_3"),("сколько 3-я стоит","shift_3"),("третья когда заканчивается","shift_3"),
    ("четвертая смена даты","shift_4"),("4-я сколько стоит","shift_4"),("четвертую когда привозить","arrival"),
    ("зимняя смена","winter"),("тайны северного сияния","winter"),("сколько зимний лагерь","winter"),
    ("питание","food"),("сколько раз кормят","food"),("что дети едят","food"),
    ("справка 079 нужна?","documents"),("какие документы","documents"),("нужен полис омс","documents"),
    ("что взять с собой","bring"),("что собрать ребенку","bring"),("какие вещи брать","bring"),
    ("какую обувь","shoes"),("что из обуви","shoes"),("обувь в лагерь","shoes"),
    ("что из гигиены","hygiene"),("нужен шампунь","hygiene"),("зубная щетка нужна","hygiene"),
    ("какую одежду","clothes"),("что из одежды","clothes"),("футболки и штаны","clothes"),
    ("во сколько заезд","arrival"),("когда выезд","arrival"),("время заезда","arrival"),
    ("входит трансфер","transfer"),("есть трансфер","transfer"),
    ("куда ехать оформлять путевку","contacts"),("номер телефона","contacts"),("как связаться","contacts"),("адрес офиса","contacts"),
    ("какие скидки","discounts"),("есть льготы","discounts"),
    ("с какого возраста","age"),("до скольки лет","age"),
    ("можно навещать ребенка","visits"),("посещения ребенка","visits"),
    ("кому отдать лекарства","medicines"),("таблетки ребенку","medicines"),
    ("если заболеет в лагере","illness"),("есть медпункт","illness"),
    ("что нельзя брать из еды","prohibited_food"),("чипсы запрещены","prohibited_food"),
    ("что запрещено брать","prohibited_items"),("что нельзя с собой","prohibited_items"),
    ("что есть на территории","territory"),("инфраструктура лагеря","territory"),
    ("есть квесты","extra_services"),("аренда беседки","extra_services"),("ночевка в корпусе","extra_services"),
    ("чем дети занимаются","program"),("какая программа","program"),("есть робототехника","program"),
    ("уведомления","notifications"),("важная рассылка","notifications"),
]

class RouterTests(unittest.TestCase):
    def test_bare_address_clarifies(self): self.assertEqual(route("адрес").clarification,"address")
    def test_unknown_none(self): self.assertIsNone(route("расскажите что-нибудь неожиданное").intent)
    def test_dynamic_availability_not_stable_intent(self): self.assertIsNone(route("есть места сейчас?").intent)
    def test_context_second_shift_price(self): self.assertEqual(route("а сколько стоит?", {"topic":"shift","entity":"shift:2"}).intent,"shift_2")
    def test_context_winter_when(self): self.assertEqual(route("а когда?", {"topic":"winter_shift","entity":"winter"}).intent,"winter")


def _make_router_test(text, expected):
    def test(self): self.assertEqual(route(text).intent, expected, text)
    return test
for i,(text,expected) in enumerate(ROUTER_CASES): setattr(RouterTests,f"test_phrase_{i:03d}",_make_router_test(text,expected))


class CoreAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self): self.td,self.tmp,self.db,self.s,self.kb,self.core=core_env()
    async def asyncTearDown(self): self.td.cleanup()
    async def test_pinned_shifts(self):
        r=await self.core.process("anything",intent="shifts_prices"); self.assertTrue(r.supported); self.assertIn("Зимняя смена",r.answer.text)
    async def test_specific_shift(self): r=await self.core.process("когда вторая",intent="shift_2"); self.assertTrue(r.supported); self.assertEqual(r.answer.faq_id,"shift-2-2026")
    async def test_food(self): r=await self.core.process("еда",intent="food"); self.assertTrue(r.supported)
    async def test_documents(self): r=await self.core.process("079",intent="documents"); self.assertTrue(r.supported)
    async def test_unknown_intent(self): r=await self.core.process("x",intent="nonsense"); self.assertFalse(r.supported)
    async def test_current_availability_escalates(self): r=await self.core.process("есть места сейчас?"); self.assertFalse(r.supported); self.assertEqual(r.answer.reason,"dynamic_availability")
    async def test_camp_address_escalates(self): r=await self.core.process("адрес лагеря звездочка"); self.assertFalse(r.supported)
    async def test_pinned_independent_of_kb(self):
        self.kb.pages=[]; r=await self.core.process("📅 Смены и цены",intent="shifts_prices"); self.assertTrue(r.supported)
    async def test_all_intents_known(self):
        for i in INTENT_FAQ: self.assertTrue(i)
    async def test_sensitive_detector(self): self.assertTrue(looks_sensitive_or_individual("у ребенка астма"))
    async def test_availability_detector(self): self.assertTrue(looks_dynamic_availability("остались свободные места?"))


class KeyboardTests(unittest.TestCase):
    def test_main_valid_json(self): json.loads(main_keyboard())
    def test_main_has_notifications(self): self.assertIn("Важные уведомления", main_keyboard())
    def test_main_has_six_core_labels(self):
        k=json.loads(main_keyboard()); labels=[b["action"]["label"] for row in k["buttons"] for b in row]; self.assertIn("📅 Смены и цены",labels); self.assertIn("📞 Контакты",labels)
    def test_feedback_keyboard(self): self.assertIn("Помогло",answer_keyboard("documents",1))
    def test_documents_link(self): self.assertIn("Скачать документы",answer_keyboard("documents",1))
    def test_address_clarification(self): self.assertIn("Офис продаж",clarification_keyboard("address"))
    def test_subscription_on(self): self.assertIn("Отключить",notification_keyboard(True))
    def test_subscription_off(self): self.assertIn("Получать",notification_keyboard(False))
    def test_admin(self): self.assertIn("Статистика",admin_keyboard())
    def test_ticket_reply(self): self.assertIn('\\"ticket_id\\":7',manager_ticket_keyboard(7))
    def test_newsletter_preview(self): self.assertIn("Отправить",newsletter_preview_keyboard(3))
    def test_carousel(self):
        t=json.loads(shifts_carousel_template()); self.assertEqual(t["type"],"carousel"); self.assertEqual(len(t["elements"]),5)


class AdapterAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td,self.tmp,self.db,self.s,self.kb,self.core=core_env(); self.op=OperatorBridge(self.s,self.db); self.a=VKAdapter(self.s,self.core,self.db,self.op,knowledge_base=self.kb)
        self.op.set_vk_sender(self.a.send_plain_from_operator); self.op.set_vk_marker(self.a.mark_support_conversation); self.op.set_vk_manager_sender(self.a.send_to_manager)
    async def asyncTearDown(self): await self.a.close(); self.td.cleanup()
    async def test_encode_list(self): self.assertEqual(self.a._encode_param([1,2,3]),"1,2,3")
    async def test_payload_decode(self): self.assertEqual(self.a._decode_payload('{"intent":"food"}')["intent"],"food")
    async def test_manager_detection(self): self.assertTrue(self.a._is_manager(544188872)); self.assertFalse(self.a._is_manager(1))
    async def test_stats_text(self): self.assertIn("СТАТИСТИКА",self.a._stats_text())
    async def test_open_empty(self): self.assertIn("нет",self.a._open_text().lower())
    async def test_bad_empty(self): self.assertIn("нет",self.a._bad_text().lower())
    async def test_profile_publish_combines(self): self.a._profile_pages=[{"x":1}]; self.a._wall_pages=[{"x":2}]; self.a._publish_external_pages(); self.assertEqual(len(self.kb.external_pages),2)
    async def test_wall_without_content_token_never_calls_api(self):
        self.a.api=AsyncMock(side_effect=AssertionError("must not call")); n=await self.a.sync_recent_wall(); self.assertEqual(n,0)
    async def test_mark_queue_calls_two_methods(self):
        self.a.api=AsyncMock(return_value=1); await self.a.mark_support_conversation(77,True); self.assertEqual(self.a.api.await_count,2)
    async def test_typing_swallow_error(self): self.a.api=AsyncMock(side_effect=RuntimeError("x")); await self.a.typing(1)
    async def test_send_retry_only_code_100(self):
        self.a.api=AsyncMock(side_effect=[VKAPIError("messages.send",100,"bad param"),123]); r=await self.a.send_message(1,"x",template="{}",intent="customer_support"); self.assertEqual(r,123); self.assertEqual(self.a.api.await_count,2)
    async def test_regular_send_does_not_include_vk_intent(self):
        self.a.api=AsyncMock(return_value=123)
        r=await self.a.send_message(1,"x")
        self.assertEqual(r,123)
        kwargs=self.a.api.await_args.kwargs
        self.assertNotIn("intent", kwargs)

    async def test_send_retries_code_943_without_intent(self):
        self.a.api=AsyncMock(side_effect=[VKAPIError("messages.send",943,"Cannot use this intent"),123])
        r=await self.a.send_message(1,"x",intent="customer_support")
        self.assertEqual(r,123)
        self.assertEqual(self.a.api.await_count,2)
        first=self.a.api.await_args_list[0].kwargs
        second=self.a.api.await_args_list[1].kwargs
        self.assertEqual(first.get("intent"),"customer_support")
        self.assertNotIn("intent",second)

    async def test_send_does_not_retry_access(self):
        self.a.api=AsyncMock(side_effect=VKAPIError("messages.send",15,"denied"));
        with self.assertRaises(VKAPIError): await self.a.send_message(1,"x")
        self.assertEqual(self.a.api.await_count,1)
    async def test_manager_command_menu(self):
        self.a.send_message=AsyncMock(return_value=1); u=VKUser(544188872,544188872,"A",None); self.assertTrue(await self.a._manager_command(u,"#меню")); self.assertTrue(self.a.send_message.awaited)
    async def test_manager_newsletter_state(self):
        self.a.send_message=AsyncMock(return_value=1); u=VKUser(544188872,544188872,"A",None); await self.a._start_newsletter(u); self.assertEqual(self.db.get_admin_state(user_id=u.user_id)[0],"newsletter_text")
    async def test_notifications_text(self):
        self.a.send_message=AsyncMock(return_value=1); u=VKUser(3,3,"A",None); await self.a._show_notifications(u); self.assertTrue(self.a.send_message.awaited)
    async def test_clarify_address(self):
        self.a.send_message=AsyncMock(return_value=1); u=VKUser(3,3,"A",None); await self.a._clarify(u,"address"); self.assertIn("адрес",self.a.send_message.await_args.args[1].lower())
    async def test_remember_context(self):
        self.a._remember(2,route("когда вторая смена")); self.assertEqual(self.db.get_context(user_id=2)["entity"],"shift:2")
    async def test_manager_ticket_reply_flow(self):
        self.a.send_message=AsyncMock(return_value=1); self.op.deliver_human_answer=AsyncMock(return_value=(True,None)); tid=self.db.create_ticket(user_id=9,chat_id=9,username=None,full_name="P",question="Q"); u=VKUser(544188872,544188872,"M",None); handled=await self.a._manager_command(u,f"#{tid} ответ"); self.assertTrue(handled); self.op.deliver_human_answer.assert_awaited()
    async def test_diagnostics_mentions_optional_posts(self): self.assertIn("DISABLED",await self.a._admin_diagnostics_text())


class ReleaseHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td,self.tmp,self.db,self.s,self.kb,self.core=core_env()
        self.op=OperatorBridge(self.s,self.db)
        self.a=VKAdapter(self.s,self.core,self.db,self.op,knowledge_base=self.kb)
        self.op.set_vk_sender(self.a.send_plain_from_operator)
        self.op.set_vk_marker(self.a.mark_support_conversation)
        self.op.set_vk_manager_sender(self.a.send_to_manager)

    async def asyncTearDown(self):
        await self.a.close()
        self.td.cleanup()

    async def test_validate_requires_message_event_for_release(self):
        self.a.api = AsyncMock(side_effect=[
            {"server":"https://lp","key":"k","ts":"1"},
            {"events":{"message_new":1,"message_event":0}},
        ])
        with self.assertRaisesRegex(RuntimeError, "message_event"):
            await self.a.validate()

    async def test_admin_menu_clears_pending_reply_state(self):
        user=VKUser(544188872,544188872,"Manager",None)
        self.db.set_admin_state(user_id=user.user_id,state="reply_ticket",payload={"ticket_id":123})
        self.a.send_message=AsyncMock(return_value=1)
        self.assertTrue(await self.a._manager_command(user,"#меню"))
        self.assertIsNone(self.db.get_admin_state(user_id=user.user_id))

    async def test_newsletter_does_not_bypass_rejected_newsletter_intent(self):
        user=VKUser(544188872,544188872,"Manager",None)
        self.db.set_subscription(user_id=100,peer_id=100,enabled=True)
        cid=self.db.create_campaign(text="Важное объявление",created_by=user.user_id,target_count=1)
        self.a.api=AsyncMock(side_effect=VKAPIError("messages.send",100,"intent rejected"))
        self.a.send_message=AsyncMock(return_value=1)
        with patch("app.vk_adapter.asyncio.sleep", new=AsyncMock()):
            await self.a._send_newsletter(user,cid)
        self.assertEqual(self.a.api.await_count,1)
        campaign=self.db.get_campaign(cid)
        self.assertEqual(int(campaign["sent_count"]),0)
        self.assertEqual(int(campaign["failed_count"]),1)


class SourcePriorityTests(unittest.TestCase):
    def test_approved_faq_outranks_recent_social_evidence(self):
        td,tmp,db,s,kb,core=core_env()
        try:
            kb.set_external_pages([{
                "url":"https://vk.ru/wall-1_1",
                "title":"post",
                "chunks":["пятиразовое питание в лагере"],
                "source_kind":"vk_wall_recent",
            }])
            evidence=kb.search("сколько раз кормят",limit=5)
            self.assertTrue(evidence)
            self.assertEqual(evidence[0].source_kind,"approved_faq")
        finally:
            td.cleanup()


class CleanRuntimeTests(unittest.TestCase):
    def test_legacy_telegram_runtime_files_are_absent(self):
        for rel in ["app/keyboards.py","scripts/test_telegram_network.py","scripts/setup_env.py","TEST_TELEGRAM_NETWORK.cmd"]:
            self.assertFalse((ROOT/rel).exists(), rel)
