"""Translation service unit tests (request #3, racing providers)."""

from __future__ import annotations

import asyncio

import pytest

from core import translation  # noqa: E402
from core.models import Article  # noqa: E402


class FakeClient:
    """HttpClient stand-in; raises if a provider actually hits network."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_text(self, url: str) -> str:
        self.calls.append(url)
        raise AssertionError("network disabled in unit test")


def make_article(lang: str = "en") -> Article:
    return Article(
        id="id-t", title="Markets slide on rate fears", summary="Investors flee.",
        content=None, source_media="Reuters",
        source_url="https://example.com/t", category="economy", region="us",
        published_at=None, fetched_at="2026-08-26T00:00:00", tags=[],
        language=lang,
    )


def patch_providers(monkeypatch: pytest.MonkeyPatch, gtx, mymemory) -> None:
    async def g(text, http, sl):
        return await asyncio.sleep(0) or gtx(text)

    async def m(text, http, sl):
        return await asyncio.sleep(0) or mymemory(text)

    async def g_wrap(text, http, sl):  # propagate exceptions naturally
        return gtx(text)

    async def m_wrap(text, http, sl):
        return mymemory(text)

    monkeypatch.setattr(translation, "_via_gtx", g_wrap)
    monkeypatch.setattr(translation, "_via_mymemory", m_wrap)


def test_needs_translation_language_rules() -> None:
    assert translation.needs_translation(make_article("en")) is True
    assert translation.needs_translation(make_article("zh-CN")) is False


def test_race_gtx_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_providers(
        monkeypatch,
        gtx=lambda t: "市场下滑",
        mymemory=lambda t: (_ for _ in ()).throw(RuntimeError("blocked")),
    )
    out = asyncio.run(translation.translate_text("Markets slide",
                                                 client=FakeClient()))
    assert out == "市场下滑"


def test_race_falls_back_when_gtx_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(t):
        raise RuntimeError("gtx unreachable")

    patch_providers(monkeypatch, gtx=blocked, mymemory=lambda t: "市场 下滑")
    out = asyncio.run(
        translation.translate_text("Markets slide", client=FakeClient(),
                                   source_lang="en")
    )
    assert "市场" in out


def test_all_fail_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(t):
        raise RuntimeError("down")

    patch_providers(monkeypatch, gtx=boom, mymemory=boom)
    with pytest.raises(RuntimeError, match="all translation providers failed"):
        asyncio.run(translation.translate_text("hello", client=FakeClient()))


def test_batch_roundtrip_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    titles = ["Alpha rises", "Beta falls", "Gamma holds"]

    def fake_join(text: str) -> str:
        mapping = dict(zip(titles, ["阿法上涨", "贝塔下跌", "伽马持稳"], strict=True))
        return "\n".join(mapping[t] for t in text.splitlines())

    def blocked(t: str) -> str:
        raise RuntimeError("down")

    patch_providers(monkeypatch, gtx=fake_join, mymemory=blocked)
    out = asyncio.run(translation.translate_titles_batch(titles))
    assert out == ["阿法上涨", "贝塔下跌", "伽马持稳"]


def test_batch_mismatch_falls_back_to_original(monkeypatch: pytest.MonkeyPatch) -> None:
    titles = ["One", "Two", "Three", "Four"]

    def partial(text: str) -> str:
        return "一\n二"

    def blocked(t: str) -> str:
        raise RuntimeError("down")

    patch_providers(monkeypatch, gtx=partial, mymemory=blocked)
    out = asyncio.run(translation.translate_titles_batch(titles))
    assert out == ["一", "二", "Three", "Four"]
