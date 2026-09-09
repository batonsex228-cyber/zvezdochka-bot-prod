from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.core import SupportCore
from app.db import Database
from app.intent_router import route
from app.knowledge import KnowledgeBase
from app.responder import StrictResponder


async def main() -> None:
    demo_dir = Path(tempfile.mkdtemp(prefix="zvezdochka-demo-"))
    runtime = ROOT / "runtime"
    db = Database(demo_dir / "demo.sqlite3")
    kb = KnowledgeBase(
        runtime / "knowledge_base.json",
        ROOT / "data" / "seed_faq.json",
        db,
        fallback_kb_file=ROOT / "data" / "knowledge_base.json",
        max_age_hours=1_000_000,
    )
    settings = Settings(
        site_url="https://zvezdaglazov.ru/new/",
        kb_file=runtime / "knowledge_base.json",
        db_file=demo_dir / "demo.sqlite3",
        crawl_max_pages=40,
        openai_api_key=None,
        openai_model="gpt-5.6-luna",
        faq_min_score=0.72,
        retrieval_min_score=0.48,
        camp_name="ДЗЛ «Звёздочка»",
        debug=False,
        show_source_links=True,
        vk_group_token="LOCAL_DEMO_ONLY",
        vk_group_id=1,
    )
    responder = StrictResponder(settings, kb)
    core = SupportCore(responder, show_source_links=True)

    print("\nZVEZDOCHKA VK-FIRST LOCAL SUPPORT DEMO")
    print("No messenger connection is used. Type EXIT to close.\n")
    while True:
        try:
            question = input("Parent > ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if not question:
            continue
        if question.upper() in {"EXIT", "QUIT", "ВЫХОД", "СТОП"}:
            break
        decision = route(question)
        if decision.clarification:
            print(f"Bot    > [WOULD CLARIFY: {decision.clarification}]\n")
            continue
        result = await core.process(question, intent=decision.intent)
        answer = result.answer
        if result.supported and result.presented:
            print(f"Bot    > {result.presented.text}")
            if answer.source:
                print(f"Source > {answer.source}")
            print(f"Mode   > AUTO ({answer.reason}, confidence={answer.confidence:.2f}, intent={decision.intent or '-'})\n")
        else:
            print("Bot    > [WOULD ESCALATE TO HUMAN]")
            print(f"Reason > {answer.reason}; confidence={answer.confidence:.2f}\n")


if __name__ == "__main__":
    asyncio.run(main())
