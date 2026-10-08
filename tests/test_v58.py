from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.knowledge import KnowledgeBase
from app.responder import StrictResponder
from app.version import VERSION
from app.vk_keyboards import answer_keyboard
from app.links import DOCUMENTS_DOWNLOAD, CHARTER_DOWNLOAD

ROOT = Path(__file__).resolve().parent.parent


def settings(tmp: Path) -> Settings:
    return Settings(
        site_url="https://zvezdaglazov.ru/", kb_file=tmp / "knowledge.json", db_file=tmp / "db.sqlite3",
        crawl_max_pages=40, openai_api_key=None, openai_model="gpt-5.6-luna", faq_min_score=0.72,
        retrieval_min_score=0.48, camp_name="ДЗЛ «Звёздочка»", debug=False, show_source_links=True,
        auto_refresh_kb=False, kb_refresh_hours=6, kb_max_age_hours=1_000_000,
        vk_group_token="test-token", vk_group_id=241267977, vk_manager_user_id=544188872,
        vk_source_domain="zvezdochkaooorazvitie",
    )


class V58DocumentKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        tmp = Path(self.td.name)
        db = Database(tmp / "db.sqlite3")
        self.kb = KnowledgeBase(tmp / "missing.json", ROOT / "data" / "seed_faq.json", db,
                                fallback_kb_file=ROOT / "data" / "knowledge_base.json", max_age_hours=1_000_000)
        self.responder = StrictResponder(settings(tmp), self.kb)

    def tearDown(self):
        self.td.cleanup()

    def ask(self, text: str):
        return asyncio.run(self.responder.answer(text))

    def test_version(self):
        self.assertEqual(VERSION, "6.0.4")

    def test_age_is_7_to_16_inclusive(self):
        a = self.ask("До скольки лет принимаете детей?")
        self.assertTrue(a.supported)
        self.assertEqual(a.faq_id, "age")
        self.assertIn("16", a.text)

    def test_lost_item_routes_to_office(self):
        a = self.ask("Ребёнок после смены забыл кепку, где забрать потеряшку?")
        self.assertTrue(a.supported)
        self.assertEqual(a.faq_id, "lost-and-found")
        self.assertIn("Ленина, 11 Г", a.text)

    def test_camp_address_is_official_document_fact(self):
        a = self.ask("Какой адрес лагеря Звёздочка?")
        self.assertTrue(a.supported)
        self.assertEqual(a.faq_id, "camp-address")
        self.assertIn("деревни Адам", a.text)

    def test_sanitary_conclusion_is_document_fact(self):
        a = self.ask("Есть сертификат СанПиН?")
        self.assertTrue(a.supported)
        self.assertEqual(a.faq_id, "sanitary-conclusion-2026")
        self.assertIn("18.20.01.000.М.000082.06.26", a.text)

    def test_allowed_food_is_separate_from_prohibited(self):
        a = self.ask("Можно передать ребенку апельсин и воду?")
        self.assertTrue(a.supported)
        self.assertEqual(a.faq_id, "allowed-food")
        self.assertIn("апельсины", a.text)

    def test_documents_include_snils_and_three_day_rule(self):
        a = self.ask("Какие документы нужны на заезд и когда делать 079/у?")
        self.assertTrue(a.supported)
        self.assertEqual(a.faq_id, "documents")
        self.assertIn("СНИЛС", a.text)
        self.assertIn("3 рабочих дня", a.text)

    def test_document_buttons_use_new_links(self):
        self.assertIn(DOCUMENTS_DOWNLOAD, answer_keyboard("documents"))
        self.assertIn(DOCUMENTS_DOWNLOAD, answer_keyboard("sanitary-conclusion-2026"))
        self.assertIn(CHARTER_DOWNLOAD, answer_keyboard("charter"))
        self.assertEqual(DOCUMENTS_DOWNLOAD, "https://disk.yandex.ru/d/FSnKYqu_gxbw3g")
        self.assertEqual(CHARTER_DOWNLOAD, "https://disk.yandex.ru/i/tPR9gfN2kBe72g")

    def test_curated_document_corpus_is_loaded(self):
        kinds = {str(x.get("source_kind")) for x in self.kb.curated_pages}
        self.assertIn("official_document", kinds)
        self.assertIn("official_vk_bio", kinds)
        self.assertIn("owner_note", kinds)
        self.assertGreaterEqual(len(self.kb.curated_pages), 10)

    def test_document_sources_work_without_fresh_website_snapshot(self):
        # Local missing snapshot must not disable owner-provided official documents.
        faq = self.kb.get_faq_by_id("lost-and-found")
        self.assertIsNotNone(faq)
        faq = self.kb.get_faq_by_id("sanitary-conclusion-2026")
        self.assertIsNotNone(faq)


if __name__ == "__main__":
    unittest.main()
