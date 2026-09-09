from __future__ import annotations

import html
import re

ANCHOR_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def html_to_vk_text(text: str) -> str:
    """Convert the shared presentation HTML to clean VK plain text.

    VK renders line breaks/emojis well. Links are kept explicitly so older clients do not lose them.
    We intentionally avoid relying on newer rich-text extensions during the first VK rollout.
    """
    def replace_anchor(match: re.Match[str]) -> str:
        url = html.unescape(match.group(1))
        label = TAG_RE.sub("", html.unescape(match.group(2))).strip()
        if url.startswith("tel:") or url.startswith("mailto:"):
            return label
        if label == url or not label:
            return url
        return f"{label}: {url}"

    out = ANCHOR_RE.sub(replace_anchor, text)
    out = re.sub(r"</?(?:b|i|u|s|code|pre|blockquote)[^>]*>", "", out, flags=re.IGNORECASE)
    out = TAG_RE.sub("", out)
    out = html.unescape(out)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
