from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .db import Database

WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
STOPWORDS = {
    "а", "и", "или", "но", "в", "во", "на", "по", "за", "из", "к", "ко", "у", "с", "со",
    "для", "до", "от", "про", "о", "об", "это", "этот", "эта", "эти", "как", "какой", "какая",
    "какие", "что", "где", "когда", "сколько", "можно", "ли", "мне", "нам", "вы", "вас", "там",
    "его", "ее", "её", "ребенок", "ребёнок", "ребенка", "ребёнка", "дети", "ребят", "подскажите",
    "пожалуйста", "добрый", "день", "вечер", "здравствуйте",
}

SHIFT_WORDS: dict[str, int] = {
    "первая": 1, "первой": 1, "первую": 1, "1": 1,
    "вторая": 2, "второй": 2, "вторую": 2, "2": 2,
    "третья": 3, "третьей": 3, "третью": 3, "3": 3,
    "четвертая": 4, "четвертой": 4, "четвертую": 4,
    "четвёртая": 4, "четвёртой": 4, "четвёртую": 4, "4": 4,
    "пятая": 5, "пятой": 5, "пятую": 5, "5": 5,
    "шестая": 6, "шестой": 6, "шестую": 6, "6": 6,
}


def norm(text: str) -> str:
    return " ".join(WORD_RE.findall(text.lower().replace("ё", "е")))


def tokens(text: str, *, drop_stopwords: bool = False) -> list[str]:
    out = norm(text).split()
    if drop_stopwords:
        out = [w for w in out if w not in STOPWORDS]
    return out


def _stemish(word: str) -> str:
    # Conservative Russian prefix-like stem. It is deliberately simple: we only need FAQ matching,
    # not linguistic analysis, and avoiding extra native dependencies makes Windows/VPS setup safer.
    if len(word) <= 4:
        return word
    endings = (
        "иями", "ями", "ами", "его", "ого", "ему", "ому", "ыми", "ими", "ая", "яя", "ое", "ее",
        "ий", "ый", "ой", "ую", "юю", "ам", "ям", "ах", "ях", "ов", "ев", "ом", "ем", "ами",
        "ить", "ать", "ять", "ется", "ются", "ет", "ют", "ут", "ит", "ят", "ать", "ять", "ы", "и",
        "а", "я", "у", "ю", "е", "о",
    )
    for end in endings:
        if word.endswith(end) and len(word) - len(end) >= 4:
            return word[: -len(end)]
    return word


def stem_tokens(text: str) -> set[str]:
    return {_stemish(w) for w in tokens(text, drop_stopwords=True)}


def token_similarity(a: str, b: str) -> float:
    A, B = stem_tokens(a), stem_tokens(b)
    if not A or not B:
        return 0.0
    inter = len(A & B)
    return (2.0 * inter) / (len(A) + len(B))


def query_coverage(query: str, text: str) -> float:
    Q, T = stem_tokens(query), stem_tokens(text)
    if not Q or not T:
        return 0.0
    return len(Q & T) / len(Q)


def char_similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def phrase_bonus(query: str, text: str) -> float:
    q, t = norm(query), norm(text)
    bonus = 0.0
    for key in [
        "смен", "пит", "корм", "документ", "справ", "заезд", "выезд", "лекар", "телефон", "адрес",
        "запрещ", "стоим", "цена", "путев", "трансфер", "скидк", "возраст", "обув", "гигиен",
        "доширак", "газиров", "чипс", "посещ", "навещ",
    ]:
        if key in q and key in t:
            bonus += 0.06
    return min(bonus, 0.24)


def extract_shift_number(text: str) -> int | None:
    n = norm(text)
    words = n.split()
    has_shift_word = any(w.startswith("смен") for w in words)
    timing_context = any(
        key in n for key in ["когда", "стоит", "цена", "начина", "заканчи", "заезд", "привоз", "путев"]
    )
    for i, w in enumerate(words):
        if w not in SHIFT_WORDS:
            continue
        # Plain digits are too ambiguous unless the word "смена" is nearby. Written ordinals like
        # "пятую когда привозить" are common follow-ups, so timing/price context is enough for them.
        if w.isdigit():
            for j, other in enumerate(words):
                if other.startswith("смен") and abs(i - j) <= 3:
                    return SHIFT_WORDS[w]
        elif has_shift_word or timing_context:
            return SHIFT_WORDS[w]
    m = re.search(r"\b([1-6])\s*[-–—]?\s*(?:я|ая)?\s*смен", n)
    return int(m.group(1)) if m else None


@dataclass
class Evidence:
    text: str
    source: str
    score: float
    source_kind: str = "website"


@dataclass
class FAQMatch:
    answer: str
    source: str
    score: float
    question: str
    faq_id: str | None = None


class KnowledgeBase:
    def __init__(
        self,
        kb_file: Path,
        seed_file: Path,
        db: Database,
        fallback_kb_file: Path | None = None,
        max_age_hours: float = 24.0,
    ):
        self.kb_file = kb_file
        self.fallback_kb_file = fallback_kb_file
        self.seed_file = seed_file
        self.db = db
        self.max_age_hours = max_age_hours
        self.seed: dict[str, Any] = json.loads(seed_file.read_text(encoding="utf-8"))
        self.pages: list[dict[str, Any]] = []
        self.external_pages: list[dict[str, Any]] = []
        self.generated_at: datetime | None = None
        self.reload()

    def reload(self) -> None:
        self.pages = []
        self.generated_at = None
        source_file = self.kb_file if self.kb_file.exists() else self.fallback_kb_file
        if source_file and source_file.exists():
            try:
                payload = json.loads(source_file.read_text(encoding="utf-8"))
                pages = payload.get("pages", [])
                if isinstance(pages, list):
                    self.pages = pages
                raw_generated = payload.get("generated_at")
                if isinstance(raw_generated, str) and raw_generated.strip():
                    dt = datetime.fromisoformat(raw_generated.replace("Z", "+00:00"))
                    self.generated_at = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                self.pages = []
                self.generated_at = None

    def set_external_pages(self, pages: list[dict[str, Any]]) -> None:
        """Replace optional secondary evidence (for example recent VK wall posts).

        External pages never make the website snapshot fresh and never validate curated seed FAQs;
        they are only available to retrieval/grounded AI as secondary evidence.
        """
        self.external_pages = list(pages)

    @property
    def is_fresh(self) -> bool:
        if not self.pages or self.generated_at is None:
            return False
        age = datetime.now(timezone.utc) - self.generated_at.astimezone(timezone.utc)
        return age.total_seconds() <= self.max_age_hours * 3600

    def _source_text(self, source: str) -> str:
        source = source.rstrip("/")
        chunks: list[str] = []
        for page in self.pages:
            page_url = str(page.get("url", "")).rstrip("/")
            if page_url == source:
                chunks.extend(str(x) for x in (page.get("chunks", []) or []))
        return norm(" ".join(chunks))

    def _seed_is_supported_by_snapshot(self, item: dict[str, Any]) -> bool:
        # Curated facts copied from an official manager dialogue may be intentionally independent
        # of the public website. They still have a review/expiry gate so an old chat cannot become
        # eternal truth. Website-backed FAQ keeps the stricter fresh-snapshot verification below.
        valid_until = str(item.get("valid_until") or "").strip()
        if valid_until:
            try:
                dt = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
                dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) >= dt.astimezone(timezone.utc):
                    return False
            except ValueError:
                return False

        if item.get("requires_snapshot") is False:
            source = str(item.get("source", ""))
            return source.startswith("official_live_dialog:")

        # Automatic answers from the website are only allowed while our snapshot is recent.
        # If the site cannot be refreshed for too long, escalation is safer than stale facts.
        if not self.is_fresh:
            return False
        checks = [str(x) for x in (item.get("verify_contains", []) or []) if str(x).strip()]
        if not checks:
            return True
        source = str(item.get("source", ""))
        haystack = self._source_text(source)
        if not haystack:
            return False
        hay_tokens = set(tokens(haystack))
        return all(set(tokens(term)) <= hay_tokens for term in checks)

    def _all_faq(self) -> list[dict[str, Any]]:
        seed = [x for x in self.seed.get("faq", []) if self._seed_is_supported_by_snapshot(x)]
        # Manual FAQ is explicitly approved by a human and does not depend on the website snapshot age.
        return seed + self.db.list_manual_faq()

    @staticmethod
    def _entity_compatible(query: str, item: dict[str, Any], candidates: list[str]) -> bool:
        q_shift = extract_shift_number(query)
        if q_shift is None:
            return True
        allowed = item.get("shift_numbers")
        if isinstance(allowed, list):
            return q_shift in allowed
        # Old/manual entries do not have structured entities. Only allow them for a numbered-shift query
        # if the same shift is explicitly present in the FAQ question/alias text.
        return any(extract_shift_number(c) == q_shift for c in candidates)

    @staticmethod
    def _intent_compatible(query: str, item: dict[str, Any]) -> bool:
        q = norm(query)
        q_stems = stem_tokens(query)

        excluded = [str(x) for x in (item.get("intent_excluded_any", []) or []) if str(x).strip()]
        for term in excluded:
            nt = norm(term)
            if (nt and nt in q) or (stem_tokens(term) & q_stems):
                return False

        required = [str(x) for x in (item.get("intent_required_any", []) or []) if str(x).strip()]
        if not required:
            return True
        for term in required:
            nt = norm(term)
            if nt and nt in q:
                return True
            if stem_tokens(term) & q_stems:
                return True
        return False

    def get_faq_by_id(self, faq_id: str) -> FAQMatch | None:
        """Return one approved FAQ by stable id, but only if its source snapshot is currently valid.

        This is used for explicit UI intents (for example the "Смены и цены" button).
        It deliberately bypasses fuzzy matching and volatile-word heuristics while preserving
        the source freshness/verification gate from _all_faq().
        """
        for item in self._all_faq():
            if str(item.get("id", "")) != faq_id:
                continue
            return FAQMatch(
                answer=str(item["answer"]),
                source=str(item.get("source", "База лагеря")),
                score=1.0,
                question=str(item.get("question", "")),
                faq_id=faq_id,
            )
        return None

    def find_faq(self, query: str) -> FAQMatch | None:
        best: FAQMatch | None = None
        qn = norm(query)
        for item in self._all_faq():
            candidates = [item.get("question", "")] + list(item.get("aliases", []) or [])
            candidates = [str(c) for c in candidates if str(c).strip()]
            if not candidates or not self._entity_compatible(query, item, candidates) or not self._intent_compatible(query, item):
                continue

            if any(norm(c) == qn for c in candidates):
                return FAQMatch(
                    answer=str(item["answer"]),
                    source=str(item.get("source", "База лагеря")),
                    score=1.0,
                    question=str(item.get("question", "")),
                    faq_id=str(item.get("id")) if item.get("id") else None,
                )

            alias_score = max(
                max(token_similarity(query, c), char_similarity(query, c) * 0.72) for c in candidates
            )
            s = alias_score + phrase_bonus(query, " ".join(candidates))

            kws = [str(k) for k in (item.get("keywords", []) or [])]
            hit = sum(1 for k in kws if norm(k) and norm(k) in qn)
            if hit:
                s += min(0.28 + (hit - 1) * 0.07, 0.42)

            # A direct multi-word alias contained in the question is very strong evidence.
            if any(len(norm(c).split()) >= 2 and norm(c) in qn for c in candidates):
                s += 0.15

            allowed = item.get("shift_numbers")
            q_shift = extract_shift_number(query)
            if q_shift is not None and isinstance(allowed, list) and len(allowed) > 1:
                s -= 0.08  # prefer a dedicated shift FAQ over the all-shifts summary
            if q_shift is None and isinstance(allowed, list) and not any(k in qn for k in ["смен", "путев", "летн"]):
                s -= 0.32  # "сколько стоит?" without an object must not silently become the first shift
            if item.get("broad") and any(k in qn for k in ["обув", "гигиен", "одежд", "шампун", "тапоч"]):
                s -= 0.22  # a specific sub-topic is better than the broad packing-list answer
            s = max(0.0, min(s, 1.0))
            if best is None or s > best.score:
                best = FAQMatch(
                    answer=str(item["answer"]),
                    source=str(item.get("source", "База лагеря")),
                    score=s,
                    question=str(item.get("question", "")),
                    faq_id=str(item.get("id")) if item.get("id") else None,
                )
        return best

    def search(self, query: str, limit: int = 5) -> list[Evidence]:
        out: list[Evidence] = []
        q_shift = extract_shift_number(query)
        pages = (self.pages if self.is_fresh else []) + self.external_pages
        source_weights = {
            "website": 1.00,
            "vk_profile": 0.82,
            "vk_wall_recent": 0.72,
        }
        for page in pages:
            source = str(page.get("url", ""))
            source_kind = str(page.get("source_kind") or "website")
            weight = source_weights.get(source_kind, 0.75)
            for chunk in page.get("chunks", []) or []:
                chunk = str(chunk)
                if q_shift is not None:
                    c_shift = extract_shift_number(chunk)
                    # If a chunk clearly talks about another specific shift, do not use it as evidence.
                    if c_shift is not None and c_shift != q_shift:
                        continue
                coverage = query_coverage(query, chunk)
                s = (0.78 * coverage + 0.22 * token_similarity(query, chunk) + phrase_bonus(query, chunk)) * weight
                if s > 0.05:
                    out.append(Evidence(chunk, source, min(s, 1.0), source_kind=source_kind))

        for item in self._all_faq():
            candidates = [str(item.get("question", ""))] + [str(x) for x in item.get("aliases", []) or []]
            if (q_shift is not None and not self._entity_compatible(query, item, candidates)) or not self._intent_compatible(query, item):
                continue
            combined = f"{item.get('question','')} {item.get('answer','')}"
            coverage = query_coverage(query, combined)
            s = 0.75 * coverage + 0.25 * token_similarity(query, combined) + phrase_bonus(query, combined)
            if s > 0.05:
                out.append(Evidence(str(item["answer"]), str(item.get("source", "База лагеря")), min(s * 1.08, 1.0), source_kind="approved_faq"))

        out.sort(key=lambda e: e.score, reverse=True)
        uniq: list[Evidence] = []
        seen: set[str] = set()
        for e in out:
            key = norm(e.text)[:600]
            if key in seen:
                continue
            seen.add(key)
            uniq.append(e)
            if len(uniq) >= limit:
                break
        return uniq
