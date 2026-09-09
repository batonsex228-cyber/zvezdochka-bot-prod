from __future__ import annotations

import re


_GREETING_RE = re.compile(
    r"^\s*(?P<greeting>"
    r"доброе\s+утро|добрый\s+день|добрый\s+вечер|"
    r"здравствуйте|здравствуй|здраствуйте|здрасте|здравия\s+желаю|"
    r"привет(?:ик)?|хай|hello"
    r")(?=$|[\s,!.?:;—–\-])\s*[,!.?:;—–\-]*\s*",
    re.IGNORECASE,
)

_CANONICAL = {
    "доброе утро": "Доброе утро",
    "добрый день": "Добрый день",
    "добрый вечер": "Добрый вечер",
    "здравствуйте": "Здравствуйте",
    "здравствуй": "Здравствуйте",
    "здраствуйте": "Здравствуйте",
    "здрасте": "Здравствуйте",
    "здравия желаю": "Здравствуйте",
    "привет": "Привет",
    "приветик": "Привет",
    "хай": "Привет",
    "hello": "Здравствуйте",
}


def split_leading_greeting(text: str) -> tuple[str | None, str]:
    """Return a canonical leading greeting and the remaining user text.

    We only treat a greeting as such when it is at the very beginning of the message.
    This prevents an ordinary question containing the word "привет" from being rewritten.
    """
    raw = (text or "").strip()
    if not raw:
        return None, ""
    match = _GREETING_RE.match(raw)
    if not match:
        return None, raw
    original = re.sub(r"\s+", " ", match.group("greeting").lower().replace("ё", "е").strip())
    return _CANONICAL.get(original, "Здравствуйте"), raw[match.end():].strip()


def is_greeting_only(text: str) -> bool:
    greeting, rest = split_leading_greeting(text)
    if not greeting:
        return False
    # Allow a wave/smile or punctuation after the greeting without treating it as a question.
    return not bool(re.search(r"[0-9A-Za-zА-Яа-яЁё]", rest))


def greeting_intro(greeting: str | None = None) -> str:
    hello = greeting or "Здравствуйте"
    return (
        f"{hello}! 👋\n\n"
        "Я помощник службы поддержки ДЗЛ «Звёздочка» ⭐\n\n"
        "Могу помочь со сменами и ценами, заездом, документами, питанием, вещами "
        "и другими вопросами о лагере.\n\n"
        "Просто напишите, что хотите узнать 😊"
    )


def attachment_intro(*, sticker: bool = False) -> str:
    if sticker:
        return greeting_intro("Здравствуйте")
    return (
        "Здравствуйте! 👋\n\n"
        "Вижу вложение 🙂 Я помощник службы поддержки ДЗЛ «Звёздочка». "
        "Если у Вас есть вопрос о лагере, напишите его сообщением — постараюсь помочь."
    )


def with_greeting(text: str, greeting: str | None) -> str:
    if not greeting:
        return text
    return f"{greeting}! 👋\n\n{text}"
