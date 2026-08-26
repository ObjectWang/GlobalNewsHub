"""Article image download & on-disk cache (user request #4).

QTextBrowser never fetches remote resources by itself — offline safety.
Instead the detail flow extracts ``<img src>`` URLs, downloads them in a
background thread through :class:`HttpClient` (proxy-aware), stores them
under ``data/image_cache/<sha256>.<ext>`` and registers them as Qt
image resources so the rendered article displays local copies.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from core.utils.network import HttpClient
from core.utils.platform_utils import data_dir

logger = logging.getLogger(__name__)

_IMG_SRC_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)

_MAX_IMAGES_DEFAULT = 8
_MAX_BYTES_PER_IMAGE = 5 * 1024 * 1024

_ALLOWED_SCHEMES = ("http://", "https://")


def extract_image_urls(html_text: str | None, limit: int = _MAX_IMAGES_DEFAULT) -> list[str]:
    """Absolute http(s) image URLs from ``html_text``, de-duplicated, capped."""
    if not html_text:
        return []
    seen: dict[str, None] = {}
    for raw in _IMG_SRC_RE.findall(html_text):
        url = raw.strip()
        if not url.lower().startswith(_ALLOWED_SCHEMES):
            continue
        seen.setdefault(url, None)
        if len(seen) >= limit:
            break
    return list(seen)


def cache_dir(base: Path | None = None) -> Path:
    """Writable image-cache directory (``data/image_cache``)."""
    return (base or data_dir()) / "image_cache"


def local_cache_path(url: str, base: Path | None = None) -> Path:
    """Deterministic cache path for ``url`` (extension guessed from URL)."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    suffix = ".img"
    match = re.search(r"\.(jpe?g|png|gif|webp|bmp)(?:[?#]|$)", url, re.IGNORECASE)
    if match:
        suffix = "." + match.group(1).lower().replace("jpeg", "jpg")
    return cache_dir(base) / f"{digest}{suffix}"


async def fetch_images_to_cache(
    urls: Iterable[str],
    *,
    client: HttpClient,
    base: Path | None = None,
    max_bytes: int = _MAX_BYTES_PER_IMAGE,
    on_saved: Callable[[str, Path], Awaitable[None]] | None = None,
) -> dict[str, str]:
    """Download missing images sequentially; return ``{url: local_path}``.

    Already-cached files are reused without network. Oversized/broken
    payloads are skipped with a warning rather than failing the batch.
    """
    mapping: dict[str, str] = {}
    for url in urls:
        path = local_cache_path(url, base)
        if not path.is_file():
            try:
                data = await client.get_bytes(url)
                if len(data) > max_bytes:
                    logger.warning("image too large (%dB), skipped: %s", len(data), url)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            except Exception as exc:
                logger.warning("image download failed %s: %s: %s",
                               url, type(exc).__name__, exc)
                continue
        mapping[url] = str(path)
        if on_saved is not None:
            await on_saved(url, path)
    return mapping
