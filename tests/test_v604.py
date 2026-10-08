"""Offline integration and regressions for optional shift application module."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from app.config import Settings
from app.db import Database
from app.shift_booking import (
    ShiftEngine, ShiftUser, requested_shift, is_shift_application_request,
    clean_name, birth_date, phone_number,
)
from app.vk_adapter import VKAdapter, VKUser
from app.vk_keyboards import manager_shift_keyboard
from app.version import VERSION


class FakeBackend:
    def __init__(self):
        self.calls=[]
        self.fail=False
    async def call(self,action,payload):
        if self.fail:
            raise OSError('unavailable')
        self.calls.append((action,dict(payload)))
        return {'saved':True}
    async def close(self):
        pass


class ShiftParsingTests(unittest.TestCase):
    def test_version(self): self.assertEqual(VERSION,'6.0.4')
    def test_ordinals(self):
        for text,n in [('первую смену',1),('на 2 смену',2),('третью смену',3),('на четвёртую смену',4),('пятую смену',5),('3-я смена',3)]:
            with self.subTest(text=text): self.assertEqual(requested_shift(text),n)
    def test_intents(self):
        for phrase in ('Добрый вечер! Хочу купить путёвку на первую смену','оформить заявку на смену','запишите ребенка на 2 смену','хочу забронировать путевку'):
            with self.subTest(phrase=phrase): self.assertTrue(is_shift_application_request(phrase))
    def test_informational_protected(self):
        for phrase in ('Сколько стоит первая смена?','Когда начинается первая смена?','Как купить путёвку?', 'Есть ли места на вторую смену?', 'Хочу вернуть путевку', 'Поменять смену', 'Хочу арендовать беседку'):
            with self.subTest(phrase=phrase): self.assertFalse(is_shift_application_request(phrase))
    def test_validators(self):
        self.assertEqual(clean_name('Иванов  Артём Сергеевич'),'Иванов Артём Сергеевич')
        self.assertIsNone(clean_name('=IMPORTXML ссылка'))
        self.assertEqual(birth_date('15.07.2015'),'15.07.2015')
        self.assertIsNone(birth_date('31.02.2015'))
        self.assertIsNone(birth_date('02.02.2033'))
        self.assertEqual(phone_number('8 (912) 123-45-67'),'+79121234567')
        self.assertIsNone(phone_number('8999'))
    def test_manager_keyboard(self):
        self.assertIn('shift_manager',manager_shift_keyboard('ZV-AABBCCDDEEFF'))


class ShiftFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.settings=Settings(site_url='https://zvezdaglazov.ru',kb_file=Path(self.temp.name)/'kb.json',db_file=Path(self.temp.name)/'bot.sqlite',crawl_max_pages=1,openai_api_key=None,openai_model='test',faq_min_score=.7,retrieval_min_score=.5,camp_name='Звездочка',debug=False,show_source_links=False,
                               vk_group_id=1,vk_group_token='FAKE',vk_manager_user_id=2,
                               shift_bookings_enabled=True,shift_privacy_policy_url='https://example.org/privacy',shift_active_numbers=(1,2,3),shift_export_pii_to_google=False)
        self.db=Database(self.settings.db_file)
        self.backend=FakeBackend()
        self.engine=ShiftEngine(self.settings,self.db,self.backend)
        self.user=ShiftUser(1001,1001)
    async def asyncTearDown(self):
        self.temp.cleanup()
    async def complete(self,shift=1):
        user=self.user
        messages=[f'Хочу купить путёвку на {shift} смену','Согласен','Иванов Артём Сергеевич','15.07.2015','Иванова Мария Александровна','8 912 123-45-67','Подтверждаю']
        results=[]
        for text in messages:
            results.append(await self.engine.handle_text(user,text))
        return results
    async def test_complete_form_local_and_sheet_no_pii(self):
        results=await self.complete()
        self.assertEqual([x.state for x in results],['consent','child_name','child_birth_date','parent_name','phone','review','submitted'])
        self.assertTrue(results[-1].application_id.startswith('ZV-'))
        self.assertIn('Документы',results[-1].text)
        self.assertIn('не гарантия места',results[-1].text)
        app=self.engine.get(results[-1].application_id)
        self.assertEqual(app['shift_number'],1)
        self.assertEqual(app['phone'],'+79121234567')
        self.assertEqual(app['status'],'new')
        self.assertEqual(app['sync_status'],'synced')
        action,data=self.backend.calls[-1]
        self.assertEqual(action,'upsert_shift_application')
        self.assertNotIn('child_name',data)
        self.assertNotIn('parent_name',data)
    async def test_export_with_dual_approval_toggle(self):
        object.__setattr__(self.settings,'shift_export_pii_to_google',True)
        last=(await self.complete(2))[-1]
        self.assertEqual(self.backend.calls[-1][1]['child_name'],'Иванов Артём Сергеевич')
        self.assertEqual(self.engine.get(last.application_id)['shift_number'],2)
    async def test_disabled_shift(self):
        response=await self.engine.handle_text(self.user,'Хочу купить путёвку на пятую смену')
        self.assertEqual(response.state,'unavailable')
        self.assertFalse(self.engine.has_active(self.user.user_id))
    async def test_choose_shift(self):
        result=await self.engine.handle_text(self.user,'Хочу оформить путёвку')
        self.assertEqual(result.state,'choose_shift')
        response=await self.engine.handle_text(self.user,'5')
        self.assertIn('не открыта',response.text)
        response=await self.engine.handle_text(self.user,'вторая смена')
        self.assertEqual(response.state,'consent')
    async def test_cancel_no_personal_data(self):
        await self.engine.handle_text(self.user,'Хочу купить путёвку на 1 смену')
        result=await self.engine.handle_text(self.user,'Нет')
        self.assertEqual(result.state,'cancelled')
        self.assertFalse(self.engine.has_active(self.user.user_id))
        self.assertEqual(self.engine.pending_apps(),[])
    async def test_invalid_phone_birth_and_correction(self):
        await self.engine.handle_text(self.user,'Хочу купить путёвку на 1 смену')
        await self.engine.handle_text(self.user,'Согласен')
        await self.engine.handle_text(self.user,'Иванов Артём Сергеевич')
        self.assertEqual((await self.engine.handle_text(self.user,'31.02.2015')).state,'child_birth_date')
        await self.engine.handle_text(self.user,'15.07.2015')
        await self.engine.handle_text(self.user,'Иванова Мария Александровна')
        self.assertEqual((await self.engine.handle_text(self.user,'0000')).state,'phone')
        await self.engine.handle_text(self.user,'+79121234567')
        response=await self.engine.handle_text(self.user,'Исправить')
        self.assertEqual(response.state,'child_name')
        self.assertEqual(self.engine.pending_apps(),[])
    async def test_payment_data_rejected(self):
        await self.engine.handle_text(self.user,'Хочу купить путёвку на 1 смену')
        await self.engine.handle_text(self.user,'Согласен')
        response=await self.engine.handle_text(self.user,'4111 1111 1111 1111')
        self.assertIn('банковской карты',response.text)
    async def test_manager_confirmation_idempotent(self):
        result=(await self.complete())[-1]
        app,updated=await self.engine.manager_decision(result.application_id,True,2)
        self.assertTrue(updated)
        self.assertEqual(app['status'],'confirmed')
        app,updated=await self.engine.manager_decision(result.application_id,False,2)
        self.assertFalse(updated)
        self.assertEqual(app['status'],'confirmed')
        self.assertEqual(self.backend.calls[-1][1]['status'],'confirmed')
    async def test_unsynced_retry(self):
        self.backend.fail=True
        res=(await self.complete())[-1]
        self.assertEqual(self.engine.get(res.application_id)['sync_status'],'pending')
        self.backend.fail=False
        self.assertEqual(await self.engine.retry_pending(),1)
        self.assertEqual(self.engine.get(res.application_id)['sync_status'],'synced')
    async def test_explicit_cancel(self):
        await self.engine.handle_text(self.user,'Хочу купить путёвку на 1 смену')
        await self.engine.handle_text(self.user,'Согласен')
        res=await self.engine.handle_text(self.user,'Отмена')
        self.assertEqual(res.state,'cancelled')
    async def test_multi_user_independence(self):
        await self.engine.handle_text(self.user,'Хочу купить путёвку на 1 смену')
        other=ShiftUser(1002,1002)
        await self.engine.handle_text(other,'Хочу купить путёвку на 2 смену')
        self.assertEqual(self.engine._session(other.user_id)['payload']['shift_number'],2)
        self.assertEqual(self.engine._session(self.user.user_id)['payload']['shift_number'],1)
    async def test_adapter_shift_route(self):
        adapter=object.__new__(VKAdapter)
        adapter.shifts=self.engine
        adapter.booking=None
        adapter.intake=None
        adapter.settings=self.settings
        adapter.send_message=AsyncMock(return_value=1)
        user=VKUser(self.user.user_id,self.user.peer_id,'Тест','test')
        ok=await adapter._try_shift_text(user,'Хочу купить путёвку на первую смену')
        self.assertTrue(ok)
        self.assertIn('обработке персональных данных',str(adapter.send_message.call_args))

if __name__=='__main__': unittest.main()
