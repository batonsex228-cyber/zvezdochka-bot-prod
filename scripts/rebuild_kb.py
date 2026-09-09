from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.crawler import crawl_site


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Safely rebuild the website knowledge snapshot.")
    parser.add_argument("--url", default=os.getenv("SITE_URL", "https://zvezdaglazov.ru/new/"))
    parser.add_argument("--out", default=os.getenv("KB_FILE", "runtime/knowledge_base.json"))
    parser.add_argument("--max-pages", type=int, default=int(os.getenv("CRAWL_MAX_PAGES", "40")))
    args = parser.parse_args()

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    payload = asyncio.run(crawl_site(args.url, out, max(1, min(args.max_pages, 200))))
    if not payload.get("pages"):
        print("No pages downloaded. Existing knowledge base was NOT overwritten.")
        return 2
    print(f"Saved {len(payload['pages'])} pages -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
