"""On-demand article translation (user request #3).

Explicit user action only — nothing is sent to network automatically
(section 9 privacy). Providers race CONCURRENTLY (first success wins)
to cut latency on networks where one endpoint is blocked:

1. Google translate public ``gtx`` endpoint (no key);
2. MyMemory anonymous API (free tier).

Both are plain HTTPS GETs routed through :class:`HttpClient`, so the
``network.proxy`` setting applies. Short timeouts keep the perceived
speed snappy; no offline MT model is bundled to protect install size.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol
from urllib.parse import quote

from core.models import Article
from core.utils.network import HttpClient

logger = logging.getLogger(__name__)

_TARGET_LANG = "zh-CN"

_TRANSLATE_TIMEOUT_S = 8  # snappy > exhaustive: providers race anyway

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
    """Body text submitted for translation (summary/lead paragraph)."""
    if article.summary and article.summary.strip():
        return article.summary.strip()[:4000]
    return (article.title or "").strip()[:4000]


class _Provider(Protocol):
    """Async provider callable: (text, http, source_lang) -> translated."""

    async def __call__(
        self, text: str, http: HttpClient, source_lang: str
    ) -> str: ...


async def _race(
    providers: tuple[_Provider, ...],
    text: str,
    http: HttpClient,
    source_lang: str,
) -> str:
    """Run providers concurrently; first non-empty success wins."""
    tasks: dict[asyncio.Task[str], _Provider] = {
        asyncio.create_task(provider(text, http, source_lang)): provider
        for provider in providers
    }
    errors: list[str] = []
    try:
        while tasks:
            done, _pending = await asyncio.wait(
                tasks.keys(), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                name = type(tasks.pop(task)).__name__
                try:
                    result = str(task.result())
                except Exception as exc:
                    logger.warning("translation provider %s failed: %s: %s",
                                   name, type(exc).__name__, exc)
                    errors.append(f"{name}: {exc}")
                    continue
                if result.strip():
                    for pending in tasks:
                        pending.cancel()
                    return result.strip()
                errors.append(f"{name}: empty result")
    finally:
        for pending in tasks:
            pending.cancel()
    raise RuntimeError("all translation providers failed: " + "; ".join(errors))


async def translate_text(
    text: str,
    *,
    client: HttpClient | None = None,
    source_lang: str = "auto",
) -> str:
    """Translate ``text`` to Chinese via racing providers."""
    http = client or HttpClient(timeout_seconds=_TRANSLATE_TIMEOUT_S, max_retry=0)
    return await _race((_via_gtx, _via_mymemory), text, http, source_lang)


async def translate_titles_batch(titles: list[str]) -> list[str]:
    """Translate many short titles in ONE request per provider.

    Titles are joined with newlines; gtx preserves line structure well.
    When the split-back count mismatches (provider merged lines), missing
    entries fall back to the original title so the UI never breaks.
    """
    joined = "\n".join(titles)
    joined_result = await _race(
        (_via_gtx, _via_mymemory), joined,
        HttpClient(timeout_seconds=_TRANSLATE_TIMEOUT_S, max_retry=0),
        "auto",
    )
    lines = [line.strip() for line in joined_result.splitlines()]
    lines = [line for line in lines if line]
    results: list[str] = []
    for i, original in enumerate(titles):
        candidate = lines[i] if i < len(lines) else ""
        results.append(candidate if candidate else original)
    return results


async def _via_gtx(text: str, http: HttpClient, source_lang: str) -> str:
    """Google gtx: response is [[["译","orig",...], ...], ...]."""
    del source_lang
    url = _GTX_URL.format(q=quote(text))
    raw = await http.get_text(url)
    data = json.loads(raw)
    segments = data[0] or []
    out_lines: list[str] = []
    current: list[str] = []
    for seg in segments:
        piece = str(seg[0]) if seg and seg[0] else ""
        if not piece:
            continue
        current.append(piece)
        if piece.endswith("\n") or "\n" in piece:
            out_lines.append("".join(current))
            current = []
    if current:
        out_lines.append("".join(current))
    # gtx collapses newlines inside one q into separate segments; rebuild
    # line parity with the request when possible.
    requested_lines = text.count("\n") + 1
    while len(out_lines) < requested_lines:
        out_lines.append("")
    return "\n".join(out_lines[:requested_lines])


async def _via_mymemory(text: str, http: HttpClient, source_lang: str) -> str:
    """MyMemory free API; needs a concrete source language pair."""
    sl = "en" if source_lang in ("auto", "", "zh-CN") else source_lang
    url = _MYMEMORY_URL.format(q=quote(text[:500]), sl=sl, tl=_TARGET_LANG)
    raw = await http.get_text(url)
    data = json.loads(raw)
    return str(data.get("responseData", {}).get("translatedText", ""))
