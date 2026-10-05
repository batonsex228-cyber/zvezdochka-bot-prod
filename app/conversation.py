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

# These are deliberately whole-message social phrases.  We do NOT use broad
# substring matching: "спасибо, а сколько стоит?" must continue to the normal
# question router instead of being swallowed as small talk.
_SOCIAL_THANKS = {
    "спасибо", "спасибо большое", "большое спасибо", "огромное спасибо",
    "спасибо вам", "спасибо за помощь", "спасибо большое за помощь",
    "благодарю", "благодарю вас", "благодарю за помощь",
    "спасибки", "спасибочки", "мерси",
    "спасибо вам большое", "спасибо огромное", "огромное вам спасибо",
    "понятно спасибо", "спасибо все понятно", "все понятно спасибо",
    "спасибо за ответ", "спасибо за информацию", "благодарю за ответ",
    "благодарю вас за ответ", "понял спасибо", "поняла спасибо",
    "ясно спасибо", "ок спасибо", "окей спасибо",
}
_SOCIAL_FAREWELL = {
    "до свидания", "до встречи", "до скорой встречи", "до скорого",
    "всего доброго", "всего хорошего", "хорошего дня", "хорошего вечера",
    "доброй ночи", "пока", "пока пока", "удачи",
    "спасибо до свидания", "спасибо всего доброго", "спасибо всего хорошего",
    "спасибо до встречи", "большое спасибо до свидания",
    "до свидания спасибо", "до встречи спасибо", "до новых встреч", "спасибо хорошего дня",
    "спасибо хорошего вечера", "благодарю всего доброго",
}
_SOCIAL_ACK = {
    "понятно", "все понятно", "ясно", "хорошо", "хорошо спасибо",
    "понял", "поняла", "поняли", "ок", "окей", "okay", "ладно",
    "принято", "договорились", "супер", "отлично", "здорово", "класс",
    "все получилось", "получилось", "разобрался", "разобралась",
    "нет спасибо", "не надо спасибо", "спасибо не надо", "спасибо не нужно",
    "спасибо но не надо", "спасибо но не нужно", "все спасибо",
    "я понял", "я поняла", "все ясно", "все хорошо", "хорошо понял", "хорошо поняла",
}
_SOCIAL_PING = {
    "ау", "алло", "бот", "вы тут", "вы здесь", "есть кто", "есть кто нибудь",
    "ты тут", "ты здесь",
}


def _social_norm(text: str) -> str:
    raw = (text or "").lower().replace("ё", "е")
    # Treat punctuation and common emoji as decoration while keeping words/numbers.
    raw = re.sub(r"[^0-9a-zа-я]+", " ", raw, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", raw).strip()


def classify_social_only(text: str) -> str | None:
    """Classify a whole-message social/etiquette utterance.

    Returns one of: thanks, farewell, acknowledgement, ping, reaction.
    Returns None whenever meaningful question-like text is present.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    n = _social_norm(raw)
    if n in _SOCIAL_FAREWELL:
        return "farewell"
    if n in _SOCIAL_THANKS:
        return "thanks"
    if n in _SOCIAL_ACK:
        return "acknowledgement"
    if n in _SOCIAL_PING:
        return "ping"

    # Emoji / punctuation-only reactions should never create a manager ticket.
    # A string consisting only of question marks is handled as a ping instead of
    # being escalated as an unsupported question.
    if not re.search(r"[0-9A-Za-zА-Яа-яЁё]", raw):
        if "?" in raw or "？" in raw:
            return "ping"
        return "reaction"
    return None


def social_reply(kind: str) -> str:
    if kind == "thanks":
        return "Всегда пожалуйста 💛 Если появятся ещё вопросы — пишите, я постараюсь помочь."
    if kind == "farewell":
        return (
            "До скорой встречи! 💛 Надеюсь, увидимся в «Звёздочке» ⭐\n\n"
            "Если под предыдущим ответом есть кнопки оценки, буду благодарен за обратную связь."
        )
    if kind == "acknowledgement":
        return "Хорошо 😊 Если появится ещё вопрос — просто напишите."
    if kind == "ping":
        return "Да, я здесь 😊 Напишите, пожалуйста, что хотите узнать — постараюсь помочь."
    return "💛 Если появится вопрос — пишите, я на связи."


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
