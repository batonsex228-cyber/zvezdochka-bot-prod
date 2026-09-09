from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx",
    ".mp4", ".webm", ".mp3", ".css", ".js",
)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    p = urlparse(urldefrag(url)[0])
    # Site pages are static; query strings are usually tracking/cache noise and cause duplicate crawling.
    return urlunparse((p.scheme, p.netloc, p.path or "/", "", "", ""))


def chunk_text(text: str, max_chars: int = 1200, overlap_chars: int = 160) -> list[str]:
    text = clean_text(text)
    if len(text) <= max_chars:
        return [text] if len(text) >= 40 else []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 <= max_chars:
            current = (current + " " + sentence).strip()
            continue
        if current:
            chunks.append(current)
        tail = current[-overlap_chars:] if current else ""
        current = (tail + " " + sentence).strip()
    if current:
        chunks.append(current)
    return [c for c in chunks if len(c) >= 40]


def extract_page(html: str, url: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else url
    root = soup.find("main") or soup.body or soup
    blocks: list[str] = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "dt", "dd"]):
        t = clean_text(el.get_text(" ", strip=True))
        if t and len(t) >= 3:
            blocks.append(t)
    unique = list(dict.fromkeys(blocks))
    return title, chunk_text("\n".join(unique))


def is_allowed(url: str, base_url: str) -> bool:
    p, b = urlparse(url), urlparse(base_url)
    if p.scheme not in ("http", "https") or p.netloc.lower() != b.netloc.lower():
        return False
    if any(p.path.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
        return False
    base_prefix = b.path.rstrip("/") + "/"
    return p.path.startswith(base_prefix) or p.path.rstrip("/") == b.path.rstrip("/")


async def _get_with_retry(client: httpx.AsyncClient, url: str, attempts: int = 2) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            r = await client.get(url)
            r.raise_for_status()
            return r
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            last = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5 * (attempt + 1))
    assert last is not None
    raise last


async def crawl_site(base_url: str, out_file: Path, max_pages: int = 40) -> dict:
    base_url = canonical_url(base_url)
    # Crawl the support-critical pages first so a page budget or noisy photo archive cannot crowd them out.
    priority = [
        base_url,
        canonical_url(urljoin(base_url, "pages/trip.html")),
        canonical_url(urljoin(base_url, "pages/parents.html")),
        canonical_url(urljoin(base_url, "pages/contacts.html")),
        canonical_url(urljoin(base_url, "pages/extra-services.html")),
    ]
    queue = deque(dict.fromkeys(priority))
    seen: set[str] = set()
    pages: list[dict] = []

    timeout = httpx.Timeout(connect=10, read=20, write=10, pool=10)
    headers = {"User-Agent": "ZvezdochkaSupportBot/5.0 (+knowledge-base-refresh)"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        while queue and len(seen) < max_pages:
            url = canonical_url(queue.popleft())
            if url in seen or not is_allowed(url, base_url):
                continue
            seen.add(url)
            try:
                r = await _get_with_retry(client, url)
                if "text/html" not in r.headers.get("content-type", "").lower():
                    continue
            except Exception as exc:
                print(f"[crawler] skip {url}: {type(exc).__name__}: {exc}")
                continue

            final_url = canonical_url(str(r.url))
            title, chunks = extract_page(r.text, final_url)
            if chunks:
                pages.append({"url": final_url, "title": title, "chunks": chunks})

            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                nxt = canonical_url(urljoin(final_url, a["href"]))
                if nxt not in seen and is_allowed(nxt, base_url):
                    queue.append(nxt)

    payload = {
        "source": base_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pages": pages,
    }

    # Atomic safety: a temporary website/network problem must never erase the previous working KB.
    if pages:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_file.with_suffix(out_file.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out_file)
    return payload


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", default="runtime/knowledge_base.json")
    parser.add_argument("--max-pages", type=int, default=40)
    args = parser.parse_args()
    payload = asyncio.run(crawl_site(args.url, Path(args.out), args.max_pages))
    if payload["pages"]:
        print(f"Saved {len(payload['pages'])} pages to {args.out}")
    else:
        print("No pages downloaded. Existing knowledge base was NOT overwritten.")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
