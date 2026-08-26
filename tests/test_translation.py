"""Translation service unit tests (request #3)."""

from __future__ import annotations

import asyncio

import pytest

from core import translation  # noqa: E402
from core.models import Article  # noqa: E402


class FakeClient:
    """HttpClient stand-in scripting ordered get_text responses."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def get_text(self, url: str) -> str:
        self.calls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_article(lang: str = "en") -> Article:
    return Article(
        id="id-t", title="Markets slide on rate fears", summary="Investors flee.",
        content=None, source_media="Reuters",
        source_url="https://example.com/t", category="economy", region="us",
        published_at=None, fetched_at="2026-08-26T00:00:00", tags=[],
        language=lang,
    )


def test_needs_translation_language_rules() -> None:
    assert translation.needs_translation(make_article("en")) is True
    assert translation.needs_translation(make_article("zh-CN")) is False


GTX_JSON = '[[["市场因利率担忧而下滑","Markets slide","",null,10]],null,"en"]'
MM_JSON = '{"responseData":{"translatedText":"市场 下滑"},"responseStatus":200}'


def test_gtx_provider_first() -> None:
    fake = FakeClient([GTX_JSON])
    out = asyncio.run(translation.translate_text("Markets slide", client=fake))
    assert out == "市场因利率担忧而下滑"
    assert "translate.googleapis.com" in fake.calls[0]


def test_fallback_to_mymemory() -> None:
    fake = FakeClient([RuntimeError("gtx blocked"), MM_JSON])
    out = asyncio.run(
        translation.translate_text("Markets slide", client=fake, source_lang="en")
    )
    assert "市场" in out
    assert len(fake.calls) == 2
    assert "mymemory" in fake.calls[1]


def test_all_providers_fail_raises() -> None:
    fake = FakeClient([RuntimeError("x"), RuntimeError("y")])
    with pytest.raises(RuntimeError, match="all translation providers failed"):
        asyncio.run(translation.translate_text("hello", client=fake))
