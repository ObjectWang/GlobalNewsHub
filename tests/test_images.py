"""Image cache + detail pane image/translation integration (requests #3/#4)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="UI extra not installed")

from core.models import Article  # noqa: E402
from core.utils.images import (  # noqa: E402
    extract_image_urls,
    fetch_images_to_cache,
    local_cache_path,
)
from ui.news_detail import NewsDetailWidget  # noqa: E402

HTML_WITH_IMG = (
    "<p>lead</p><img src='https://cdn.example.com/a.jpg'>"
    "<img src='data:bad,xx'><img src='https://cdn.example.com/b.png?w=200'>"
    "<img src='https://cdn.example.com/a.jpg'>"
)


class FakeBytesClient:
    """HttpClient stand-in returning scripted byte payloads."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    async def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        data = self.payloads[url]
        if isinstance(data, Exception):
            raise data
        return data


def make_article(**over) -> Article:
    base = dict(
        id="id-9", title="Tech roundup", summary=None,
        content=HTML_WITH_IMG, source_media="The Verge",
        source_url="https://example.com/9", category="tech", region="us",
        published_at=None, fetched_at="2026-08-26T00:00:00", tags=[],
        language="en",
    )
    base.update(over)
    return Article(**base)  # type: ignore[arg-type]


# ---- pure helpers ---------------------------------------------------------

def test_extract_dedupes_filters_and_caps() -> None:
    urls = extract_image_urls(HTML_WITH_IMG, limit=5)
    assert urls == [
        "https://cdn.example.com/a.jpg",
        "https://cdn.example.com/b.png?w=200",
    ]


def test_cache_path_stable_and_extension(tmp_path: Path) -> None:
    p1 = local_cache_path("https://x/a.jpg?sig=1", tmp_path)
    p2 = local_cache_path("https://x/a.jpg?sig=1", tmp_path)
    assert p1 == p2 and p1.suffix == ".jpg" and p1.is_relative_to(tmp_path)


def test_fetch_skips_existing_and_persists(tmp_path: Path) -> None:
    payload = {"https://cdn.example.com/a.jpg": b"\xff\xd8fakejpg"}
    client = FakeBytesClient(payload)
    mapping = asyncio.run(
        fetch_images_to_cache(["https://cdn.example.com/a.jpg"],
                              client=client, base=tmp_path)
    )
    assert len(mapping) == 1
    assert Path(mapping["https://cdn.example.com/a.jpg"]).read_bytes()[:2] == b"\xff\xd8"

    client2 = FakeBytesClient({})
    mapping2 = asyncio.run(
        fetch_images_to_cache(["https://cdn.example.com/a.jpg"],
                              client=client2, base=tmp_path)
    )
    assert mapping2 and client2.calls == []  # served from cache


# ---- detail widget --------------------------------------------------------

def test_detail_emits_images_request_for_remote_imgs(qapp) -> None:
    view = NewsDetailWidget()
    got: list[tuple] = []
    view.images_requested.connect(lambda aid, urls: got.append((aid, urls)))
    view.show_article(make_article())
    assert got and got[0][0] == "id-9"
    assert len(got[0][1]) == 2


def test_translate_button_visibility_and_signal(qapp) -> None:
    view = NewsDetailWidget()
    clicked: list[tuple] = []
    view.translate_requested.connect(lambda aid, text: clicked.append((aid, text)))

    view.show_article(make_article(language="zh-CN"))
    assert not view._translate_btn.isVisibleTo(view)

    view.show_article(make_article(language="en"))
    assert view._translate_btn.isVisibleTo(view)
    view._translate_btn.click()
    assert clicked and clicked[0][0] == "id-9"
    assert "Tech roundup" in clicked[0][1]


def test_apply_translation_prepends_block(qapp) -> None:
    view = NewsDetailWidget()
    view.show_article(make_article(language="en"))
    before = view.to_plain_text()
    view.apply_translation("id-9", "这是翻译后的中文内容")
    after = view.to_plain_text()
    assert "这是翻译后的中文内容" in after
    assert "lead" in after and "阅读原文" in after  # original preserved below
    assert before.strip() != ""


def test_apply_local_image_wrong_id_ignored(qapp) -> None:
    view = NewsDetailWidget()
    view.show_article(make_article(language="en"))
    html_before = view.to_html()
    view.apply_local_image("other-id", "https://cdn.example.com/a.jpg", "x.jpg")
    assert view.to_html() == html_before
