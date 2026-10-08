"""Optional v6.0.4 summer-shift applications (not ticket sales or seat allocation).

Child details are held in the local protected SQLite database; exporting identifying
columns to Google requires an explicit separate configuration toggle.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from typing import Any

from .config import Settings
from .db import Database
from .links import DOCUMENTS_DOWNLOAD


def norm(value: str) -> str:
    return ' '.join((value or '').lower().replace('ё', 'е').split())


def requested_shift(text: str) -> int | None:
    t = norm(text)
    match = re.search(r'(?<!\d)([1-5])\s*(?:-?\s*(?:я|й|ую|ой))?\s*смен\w*', t)
    if match:
        return int(match.group(1))
    words = [
        ('перв', 1), ('втор', 2), ('трет', 3), ('треть', 3),
        ('четверт', 4), ('пят', 5),
    ]
    for root, number in words:
        if re.search(r'\b' + root + r'\w*\s+смен\w*', t):
            return number
    if t in {'1','2','3','4','5'}:
        return int(t)
    return None


def is_shift_application_request(text: str) -> bool:
    t = norm(text)
    if not t or any(k in t for k in ('вернуть', 'возврат', 'отменить путев', 'перенести', 'поменять смен', 'не прошла оплата')):
        return False
    if any(t.startswith(x) for x in ('сколько', 'когда', 'где', 'какие', 'какая цена', 'есть ли', 'остались ли', 'можно ли', 'как купить', 'как оформ', 'как забронировать')):
        return False
    if not any(k in t for k in ('путев', 'смен', 'лагер')):
        return False
    action = ('хочу купить','хотим купить','купить путев', 'оформить путев','оформить заявку',
              'оставить заявку','подать заявку', 'забронировать путев','забронировать смен',
              'бронь на смен', 'запишите на', 'записать на смен', 'запишите ребенка',
              'хочу путев','хотим путев', 'хочу записать','хочу забронировать',
              'хотим забронировать','хочу оформить','хотим оформить','заявка на смен')
    return any(x in t for x in action)


def clean_name(text: str) -> str | None:
    t = ' '.join((text or '').strip().split())
    words = t.split()
    if len(t) > 120 or not 2 <= len(words) <= 4:
        return None
    if not all(re.fullmatch(r"[А-Яа-яЁё]+(?:[-'][А-Яа-яЁё]+)?", s) and len(s) >= 2 for s in words):
        return None
    return t


def birth_date(text: str) -> str | None:
    t = (text or '').strip()
    try:
        match = re.fullmatch(r'(\d{1,2})[./-](\d{1,2})[./-](\d{4})', t)
        if not match:
            return None
        value = date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        now = date.today()
        if value > now or value < date(now.year - 19, 1, 1):
            return None
        return value.strftime('%d.%m.%Y')
    except ValueError:
        return None


def phone_number(text: str) -> str | None:
    digits = re.sub(r'\D', '', text or '')
    if len(digits) != 11 or digits[0] not in '78':
        return None
    return '+7' + digits[1:]


def payment_secret(text: str) -> bool:
    t = text or ''
    return bool(re.search(r'(?<!\d)(?:\d[\s-]?){16,19}(?!\d)', t) or re.search(r'(?i)\b(?:cvv|cvc|смс|sms|код)\b.{0,20}\d{3,8}\b', t))


@dataclass(frozen=True)
class ShiftUser:
    user_id: int
    peer_id: int


@dataclass
class ShiftResult:
    handled: bool
    text: str = ''
    state: str = ''
    application_id: str = ''
    manager_text: str = ''


STEPS = ('consent','child_name','child_birth_date','parent_name','phone','review')
STATUS_RU = {'new':'Новая', 'in_progress':'В работе', 'confirmed':'Подтверждена', 'cancelled':'Отменена'}


class ShiftEngine:
    def __init__(self, settings: Settings, db: Database, backend: Any | None = None):
        self.settings = settings
        self.db = db
        self.backend = backend

    def _session(self, user_id: int) -> dict | None:
        with self.db._conn() as conn:
            row = conn.execute('SELECT * FROM shift_sessions WHERE user_id=? AND platform=?', (user_id,'vk')).fetchone()
            if not row:
                return None
            if datetime.fromisoformat(row['expires_at']) < datetime.now(timezone.utc):
                conn.execute('DELETE FROM shift_sessions WHERE user_id=? AND platform=?',(user_id,'vk'))
                return None
            return dict(row) | {'payload':json.loads(row['payload_json'])}

    def has_active(self, user_id: int) -> bool:
        s = self._session(user_id)
        return bool(s and s['step'] != 'submitted')

    def candidate(self, text: str, *, user_id: int) -> bool:
        return self.has_active(user_id) or is_shift_application_request(text)

    def abandon(self, user_id: int) -> None:
        with self.db._conn() as conn:
            conn.execute('DELETE FROM shift_sessions WHERE user_id=? AND platform=?',(user_id,'vk'))

    def _save(self, user: ShiftUser, step: str, payload: dict) -> None:
        expires = (datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()
        with self.db._conn() as conn:
            conn.execute('''INSERT INTO shift_sessions(platform,user_id,peer_id,step,payload_json,expires_at)
              VALUES('vk',?,?,?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET
              peer_id=excluded.peer_id,step=excluded.step,payload_json=excluded.payload_json,
              expires_at=excluded.expires_at,updated_at=CURRENT_TIMESTAMP''',
              (user.user_id,user.peer_id,step,json.dumps(payload,ensure_ascii=False),expires))

    def _shift_label(self, number:int) -> str:
        return f'смену №{number}'

    def _choose_prompt(self) -> str:
        opts = ', '.join(str(n) for n in self.settings.shift_active_numbers)
        return f'🌟 На какую смену хотите подать заявку? Напишите номер смены: {opts}.\nДоступность места окончательно подтвердит менеджер.'

    def _intro(self, number:int) -> str:
        url = self.settings.shift_privacy_policy_url
        return (f'🌟 Оформляем заявку на {self._shift_label(number)}.\n\n'
                'Чтобы передать заявку менеджеру лагеря, мы запросим ФИО и дату рождения ребёнка, ФИО родителя и номер телефона.\n'
                f'Информация об обработке персональных данных: {url}\n\n'
                'Согласны на обработку этих данных для оформления заявки? Напишите «Согласен» или «Нет».')

    def _review(self, data: dict) -> str:
        return (f'🌟 Проверьте заявку на {self._shift_label(int(data["shift_number"]))}:\n'
                f'Ребёнок: {data["child_name"]}\n'
                f'Дата рождения: {data["child_birth_date"]}\n'
                f'Родитель: {data["parent_name"]}\n'
                f'Телефон: {data["phone"]}\n\n'
                'Если всё правильно, напишите «Подтверждаю». Чтобы исправить — «Исправить» (анкета начнётся сначала), или «Отмена».')

    async def handle_text(self, user:ShiftUser, text:str) -> ShiftResult:
        t=norm(text)
        s=self._session(user.user_id)
        if s and t in ('отмена','отменить','стоп','не надо','нет, отменить'):
            self.abandon(user.user_id)
            return ShiftResult(True,'Анкета отменена. Новая заявка не создана.','cancelled')
        if s and s["step"] == "submitted" and is_shift_application_request(text):
            self.abandon(user.user_id)
            s = None
        if not s:
            if not is_shift_application_request(text):
                return ShiftResult(False)
            number=requested_shift(text)
            if number is not None and number not in self.settings.shift_active_numbers:
                return ShiftResult(True, 'На эту смену сейчас нельзя автоматически подать заявку. Напишите «Смены и цены» или уточните возможность у менеджера.','unavailable')
            payload={'shift_number':number} if number else {}
            step='consent' if number else 'choose_shift'
            self._save(user,step,payload)
            return ShiftResult(True,self._intro(number) if number else self._choose_prompt(),step)
        data=s['payload']; step=s['step']
        if step=='submitted':
            return ShiftResult(True,'Заявка уже отправлена.','submitted')
        if step=='choose_shift':
            number=requested_shift(text)
            if number is None:
                return ShiftResult(True,self._choose_prompt(),step)
            if number not in self.settings.shift_active_numbers:
                return ShiftResult(True,'Эта смена сейчас не открыта для новых заявок. '+self._choose_prompt(),step)
            data['shift_number']=number
            self._save(user,'consent',data)
            return ShiftResult(True,self._intro(number),'consent')
        if step=='consent':
            if t in ('нет','не согласен','не согласна','отказываюсь'):
                self.abandon(user.user_id)
                return ShiftResult(True,'Понимаю. Без согласия заявку с персональными данными не создаём. Можно обратиться к менеджеру напрямую.','cancelled')
            if t not in ('согласен','согласна','да, согласен','да, согласна','даю согласие','принимаю'):
                return ShiftResult(True,'Чтобы продолжить, напишите «Согласен» или «Нет».','consent')
            data['consent_at']=datetime.now(timezone.utc).isoformat()
            self._save(user,'child_name',data)
            return ShiftResult(True,'Напишите, пожалуйста, ФИО ребёнка полностью (фамилия, имя, отчество).','child_name')
        if payment_secret(text):
            return ShiftResult(True,'Не отправляйте данные банковской карты или SMS-коды. Пожалуйста, ответьте только на вопрос анкеты.',step)
        if step=='child_name':
            name=clean_name(text)
            if not name:
                return ShiftResult(True,'Укажите ФИО ребёнка: фамилия, имя и отчество, если оно есть.','child_name')
            data['child_name']=name
            self._save(user,'child_birth_date',data)
            return ShiftResult(True,'Укажите дату рождения ребёнка в формате ДД.ММ.ГГГГ (например, 15.07.2015).','child_birth_date')
        if step=='child_birth_date':
            value=birth_date(text)
            if not value:
                return ShiftResult(True,'Не получилось распознать дату. Напишите дату рождения ребёнка как ДД.ММ.ГГГГ.','child_birth_date')
            data['child_birth_date']=value
            self._save(user,'parent_name',data)
            return ShiftResult(True,'Напишите полностью ФИО родителя или законного представителя.','parent_name')
        if step=='parent_name':
            name=clean_name(text)
            if not name:
                return ShiftResult(True,'Напишите ФИО родителя: фамилия, имя и отчество, если оно есть.','parent_name')
            data['parent_name']=name
            self._save(user,'phone',data)
            return ShiftResult(True,'Оставьте контактный номер телефона в формате +7 900 123-45-67.','phone')
        if step=='phone':
            value=phone_number(text)
            if not value:
                return ShiftResult(True,'Нужен номер телефона из 11 цифр, например +7 900 123-45-67.','phone')
            data['phone']=value
            self._save(user,'review',data)
            return ShiftResult(True,self._review(data),'review')
        if step=='review':
            if t in ('исправить','изменить','ошибка','нет, исправить'):
                self._save(user,'child_name',{'shift_number':data['shift_number'],'consent_at':data['consent_at']})
                return ShiftResult(True,'Исправим! Напишите ФИО ребёнка полностью.','child_name')
            if t not in ('подтверждаю','все верно','всё верно','отправить заявку','отправить'):
                return ShiftResult(True,'Напишите «Подтверждаю», «Исправить» или «Отмена».','review')
            application=self._submit(user)
            await self.sync_one(application['application_id'])
            return ShiftResult(True,
              f'✅ Заявка № {application["application_id"]} принята!\nЭто пока не оплата путёвки и не гарантия места — менеджер свяжется с Вами и подтвердит возможность оформления.\n\n📄 Документы и бланки: {DOCUMENTS_DOWNLOAD}',
              'submitted', application['application_id'],self.manager_message(application))
        return ShiftResult(False)

    def _submit(self,user:ShiftUser)->dict:
        application_id='ZV-'+uuid.uuid4().hex[:12].upper()
        with self.db._conn() as conn:
            row=conn.execute('SELECT * FROM shift_sessions WHERE platform=? AND user_id=?',('vk',user.user_id)).fetchone()
            if not row or row['step'] not in ('review','submitted'):
                raise ValueError('Сессия оформления уже закрыта')
            if row['step']=='submitted':
                old=conn.execute('SELECT * FROM shift_applications WHERE application_id=?',(json.loads(row['payload_json']).get('application_id'),)).fetchone()
                if old:
                    return dict(old)
                raise ValueError('Заявка уже отправлена')
            data=json.loads(row['payload_json'])
            assert all(data.get(k) for k in ('consent_at','child_name','child_birth_date','parent_name','phone','shift_number'))
            conn.execute('''INSERT INTO shift_applications(application_id,user_id,peer_id,shift_number,child_name,child_birth_date,parent_name,phone,consent_at)
                            VALUES(?,?,?,?,?,?,?,?,?)''',
                         (application_id,user.user_id,user.peer_id,int(data['shift_number']),data['child_name'],data['child_birth_date'],data['parent_name'],data['phone'],data['consent_at']))
            conn.execute('UPDATE shift_sessions SET step=?,payload_json=?,updated_at=CURRENT_TIMESTAMP WHERE platform=? AND user_id=?',
                         ('submitted',json.dumps({'application_id':application_id}), 'vk',user.user_id))
            result=conn.execute('SELECT * FROM shift_applications WHERE application_id=?',(application_id,)).fetchone()
            return dict(result)

    def get(self,app_id:str)->dict | None:
        with self.db._conn() as conn:
            row=conn.execute('SELECT * FROM shift_applications WHERE application_id=?',(app_id,)).fetchone()
            return dict(row) if row else None

    def manager_message(self,app:dict)->str:
        return (f'🌟 Новая заявка на путёвку № {app["application_id"]}\n'
                f'Смена: {app["shift_number"]}\nРебёнок: {app["child_name"]}\n'
                f'Дата рождения: {app["child_birth_date"]}\nРодитель: {app["parent_name"]}\n'
                f'Телефон: {app["phone"]}\nСтатус: Новая\n'
                'Подтверждение заявки не означает, что путёвка оплачена.')

    async def sync_one(self,app_id:str)->bool:
        app=self.get(app_id)
        if not app or not self.backend:
            return False
        fields={k:app[k] for k in ('application_id','shift_number','user_id','peer_id','status','payment_status','manager_id','created_at','updated_at')}
        fields['export_pii']=self.settings.shift_export_pii_to_google
        if self.settings.shift_export_pii_to_google:
            fields.update({k:app[k] for k in ('child_name','child_birth_date','parent_name','phone')})
        try:
            result=await self.backend.call('upsert_shift_application',fields)
            if not result.get('saved'):
                return False
        except Exception:
            return False
        with self.db._conn() as conn:
            conn.execute('UPDATE shift_applications SET sync_status=? WHERE application_id=? AND updated_at=?',
                         ('synced',app_id,app['updated_at']))
        return True

    async def retry_pending(self,limit:int=15)->int:
        with self.db._conn() as conn:
            rows=conn.execute("SELECT application_id FROM shift_applications WHERE sync_status != 'synced' ORDER BY created_at LIMIT ?",(limit,)).fetchall()
        sent=0
        for row in rows:
            if await self.sync_one(str(row['application_id'])):
                sent+=1
        return sent

    def pending_apps(self,limit:int=10)->list[dict]:
        with self.db._conn() as conn:
            return [dict(x) for x in conn.execute("SELECT * FROM shift_applications WHERE status IN ('new','in_progress') ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()]

    async def manager_decision(self,app_id:str,approve:bool,manager_id:int)->tuple[dict|None,bool]:
        next_status='confirmed' if approve else 'cancelled'
        with self.db._conn() as conn:
            row=conn.execute('SELECT * FROM shift_applications WHERE application_id=?',(app_id,)).fetchone()
            if not row:
                return None,False
            if row['status'] in ('confirmed','cancelled'):
                return dict(row),False
            conn.execute('''UPDATE shift_applications SET status=?,manager_id=?,sync_status='pending',updated_at=CURRENT_TIMESTAMP WHERE application_id=? AND status IN ('new','in_progress')''',
                         (next_status,manager_id,app_id))
        app=self.get(app_id)
        await self.sync_one(app_id)
        return app,True
