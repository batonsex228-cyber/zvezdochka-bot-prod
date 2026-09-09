from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _path_env(name: str, default: str) -> Path:
    raw = os.getenv(name, default).strip()
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Settings:
    site_url: str
    kb_file: Path
    db_file: Path
    crawl_max_pages: int
    openai_api_key: str | None
    openai_model: str
    faq_min_score: float
    retrieval_min_score: float
    camp_name: str
    debug: bool
    show_source_links: bool
    auto_refresh_kb: bool = True
    kb_refresh_hours: float = 6.0
    kb_max_age_hours: float = 24.0

    # VK-FIRST runtime. These two credentials are the only mandatory messenger settings.
    vk_group_token: str | None = None
    vk_group_id: int = 0
    vk_api_version: str = "5.199"
    vk_manager_user_id: int = 0

    # Official public community used as secondary profile evidence.
    vk_source_domain: str = "zvezdochkaooorazvitie"

    # Future wall import. Intentionally optional: the bot must launch without it.
    vk_content_token: str | None = None
    vk_source_post_limit: int = 10
    vk_source_max_age_days: int = 60

    # Conversation quality / admin features.
    context_ttl_minutes: int = 15
    newsletters_enabled: bool = True


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")

    try:
        vk_group_id = int(os.getenv("VK_GROUP_ID", "0") or 0)
        vk_manager_user_id = int(os.getenv("VK_MANAGER_USER_ID", "0") or 0)
    except ValueError as exc:
        raise RuntimeError("VK_GROUP_ID и VK_MANAGER_USER_ID должны быть числовыми ID без минуса/id.") from exc

    vk_token = os.getenv("VK_GROUP_TOKEN", "").strip() or None
    if not vk_token or vk_group_id <= 0:
        raise RuntimeError(
            "VK_GROUP_TOKEN и VK_GROUP_ID обязательны для v5.7 VK-FIRST. "
            "Добавьте их в .env или GitHub Codespaces Secrets."
        )

    faq_min_score = float(os.getenv("FAQ_MIN_SCORE", "0.72"))
    retrieval_min_score = float(os.getenv("RETRIEVAL_MIN_SCORE", "0.48"))
    if not 0 <= faq_min_score <= 1 or not 0 <= retrieval_min_score <= 1:
        raise RuntimeError("FAQ_MIN_SCORE и RETRIEVAL_MIN_SCORE должны быть от 0 до 1.")

    site_url = os.getenv("SITE_URL", "https://zvezdaglazov.ru/new/").strip()
    parsed = urlparse(site_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("SITE_URL должен быть корректным http/https URL.")

    return Settings(
        site_url=site_url,
        kb_file=_path_env("KB_FILE", "runtime/knowledge_base.json"),
        db_file=_path_env("DB_FILE", "runtime/bot.sqlite3"),
        crawl_max_pages=max(1, min(int(os.getenv("CRAWL_MAX_PAGES", "40")), 200)),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip() or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna",
        faq_min_score=faq_min_score,
        retrieval_min_score=retrieval_min_score,
        camp_name=os.getenv("CAMP_NAME", "ДЗЛ «Звёздочка»").strip(),
        debug=_bool_env("DEBUG", False),
        show_source_links=_bool_env("SHOW_SOURCE_LINKS", True),
        auto_refresh_kb=_bool_env("AUTO_REFRESH_KB", True),
        kb_refresh_hours=max(1.0, float(os.getenv("KB_REFRESH_HOURS", "6"))),
        kb_max_age_hours=max(1.0, float(os.getenv("KB_MAX_AGE_HOURS", "24"))),
        vk_group_token=vk_token,
        vk_group_id=vk_group_id,
        vk_api_version=os.getenv("VK_API_VERSION", "5.199").strip() or "5.199",
        vk_manager_user_id=vk_manager_user_id,
        vk_source_domain=os.getenv("VK_SOURCE_DOMAIN", "zvezdochkaooorazvitie").strip() or "zvezdochkaooorazvitie",
        vk_content_token=os.getenv("VK_CONTENT_TOKEN", "").strip() or None,
        vk_source_post_limit=max(1, min(int(os.getenv("VK_SOURCE_POST_LIMIT", "10")), 100)),
        vk_source_max_age_days=max(1, min(int(os.getenv("VK_SOURCE_MAX_AGE_DAYS", "60")), 365)),
        context_ttl_minutes=max(1, min(int(os.getenv("CONTEXT_TTL_MINUTES", "15")), 120)),
        newsletters_enabled=_bool_env("NEWSLETTERS_ENABLED", True),
    )
