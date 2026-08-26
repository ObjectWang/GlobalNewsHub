"""On-demand article translation (user request #3).

Explicit user action only — nothing is sent to network automatically
(section 9 privacy). Provider chain, first success wins:

1. Google translate public ``gtx`` endpoint (no key);
2. MyMemory anonymous API (free tier).

Both are plain HTTPS GETs routed through :class:`HttpClient`, so the
``network.proxy`` setting applies. No offline MT model is bundled to
keep the install-size budget (section 1.3).
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

from core.models import Article
from core.utils.network import HttpClient

logger = logging.getLogger(__name__)

_TARGET_LANG = "zh-CN"

_GTX_URL = (
    "https://translate.googleapis.com/translate_a/single"
    "?client=gtx&sl=auto&tl=zh-CN&dt=t&q={q}"
)

_MYMEMORY_URL = "https://api.mymemory.translated.net/get?q={q}&langpair={sl}|{tl}"


def needs_translation(article: Article) -> bool:
    """True when the article is non-Chinese and has readable content."""
    if article.language.lower().startswith("zh"):
        return False
    return bool((article.title or "").strip())


def source_text(article: Article) -> str:
    """Text submitted for translation (title + summary/body preview)."""
    parts = [article.title]
    if article.summary:
        parts.append(article.summary)
    text = "\n".join(part for part in parts if part)
    return text[:4000]


async def translate_text(
    text: str,
    *,
    client: HttpClient | None = None,
    source_lang: str = "auto",
) -> str:
    """Translate ``text`` to Chinese; tries each provider in order."""
    http = client or HttpClient(timeout_seconds=20, max_retry=1)
    errors: list[str] = []
    for provider in (_via_gtx, _via_mymemory):
        try:
            translated = await provider(text, http, source_lang)
            if translated.strip():
                return translated.strip()
        except Exception as exc:
            logger.warning("translation provider failed: %s: %s",
                           type(exc).__name__, exc)
            errors.append(f"{provider.__name__}: {exc}")
    raise RuntimeError("all translation providers failed: " + "; ".join(errors))


async def _via_gtx(text: str, http: HttpClient, source_lang: str) -> str:
    """Google gtx: response is [[["译","orig",...], ...], ...]."""
    del source_lang
    url = _GTX_URL.format(q=quote(text))
    raw = await http.get_text(url)
    data = json.loads(raw)
    segments = data[0] or []
    return "".join(str(seg[0]) for seg in segments if seg and seg[0])


async def _via_mymemory(text: str, http: HttpClient, source_lang: str) -> str:
    """MyMemory free API; needs a concrete source language pair."""
    sl = "en" if source_lang in ("auto", "", "zh-CN") else source_lang
    url = _MYMEMORY_URL.format(q=quote(text[:500]), sl=sl, tl=_TARGET_LANG)
    raw = await http.get_text(url)
    data = json.loads(raw)
    return str(data.get("responseData", {}).get("translatedText", ""))
