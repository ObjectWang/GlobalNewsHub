"""Background QThread workers (PRD task P4.8).

All database IO, network fetching and model inference happen here —
never on the UI thread (section 1.2 hard constraint). Workers own their
thread-local :class:`~core.storage.database.Database` connections
(connections must not cross threads) and report back via queued signal
deliveries only.

Contracts consumed by ``main.py``:

- RefreshWorker: sources -> orchestrator -> classify -> store;
  ``articles_stored(list[Article])`` then ``finished_ok(int)``.
- QueryWorker factory methods: list / search / detail reads;
  single ``results_ready(list[Article])`` reply.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from threading import Thread

from PySide6.QtCore import QObject, Signal

from core import translation
from core.classifier.bert_classifier import BertClassifier
from core.classifier.pipeline import ClassificationPipeline
from core.classifier.region_classifier import RegionClassifier
from core.constants import CATEGORIES, REGIONS
from core.fetcher.fetch_orchestrator import (
    PUBLIC_FALLBACK_URL,
    FetchOrchestrator,
    load_sources,
)
from core.models import Article
from core.storage.crud import get_article, insert_article, list_articles, search_articles
from core.storage.database import Database
from core.utils.images import fetch_images_to_cache
from core.utils.network import HttpClient

logger = logging.getLogger(__name__)


def classify_article(
    article: Article,
    pipeline: ClassificationPipeline | None = None,
    region: RegionClassifier | None = None,
) -> None:
    """Fill category/region in place using offline tiers when no models.

    With ``pipeline``/``region`` omitted this still applies the keyword
    rules and the domain/geo region tiers (P3.4/P3.5 degrade modes), so a
    fresh install without INT8 artifacts classifies sensibly. Model tiers
    activate automatically once artifacts exist under models_dir.
    """
    active_pipeline = pipeline or ClassificationPipeline(None)
    decision = active_pipeline.classify_category(article.title, article.summary or "")
    if article.category not in CATEGORIES or decision.decided_by != "default":
        article.category = decision.label

    active_region = region or RegionClassifier()
    text = f"{article.title} {article.summary or ''}"
    region_decision = active_region.classify(article.source_media, text)
    if article.region not in REGIONS or region_decision.region != "unknown":
        article.region = region_decision.region


class _ThreadWorker(QObject):
    """QObject worker executed on a plain ``threading.Thread``.

    Signals emitted from the python thread are delivered queued to the
    main thread, exactly like a QThread worker — but the wrapper has no
    QThread C++ lifetime semantics, which proved fragile at interpreter
    teardown on Windows (P5.1 investigation). ``wait()``/``is_running()``
    mirror the QThread surface used by :mod:`main`.
    """

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)
        self._thread: Thread | None = None

    def start(self) -> None:
        """Launch ``run_impl`` on a daemon thread (idempotent while alive)."""
        if self.is_running():
            return
        self._thread = Thread(target=self._guarded_run, daemon=True)
        self._thread.start()

    def _guarded_run(self) -> None:
        try:
            self.run_impl()
        except Exception:  # pragma: no cover - logged by implementations
            logger.exception("%s failed", type(self).__name__)

    def run_impl(self) -> None:
        """Override with the blocking workload."""
        raise NotImplementedError

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self, timeout_ms: int | None = None) -> bool:
        timeout_s = None if timeout_ms is None else max(0.0, timeout_ms / 1000)
        if self._thread is None:
            return True
        self._thread.join(timeout_s)
        return not self._thread.is_alive()


class RefreshWorker(_ThreadWorker):
    """Fetch every enabled source, classify and store; fully off-UI."""

    progress = Signal(str)
    articles_stored = Signal(list)
    refresh_report = Signal(list)
    finished_ok = Signal(int)
    failed = Signal(str)

    def __init__(
        self,
        *,
        db_path: Path,
        sources_path: Path,
        models_dir: Path | None = None,
        public_fallback_url: str = PUBLIC_FALLBACK_URL,
        max_concurrent_fetches: int = 5,
        proxy: str | None = None,
        rsshub_port: int = 1200,
        autostart_rsshub: bool = False,
        rsshub=None,
        rss_fetch=None,
        rsshub_fetch=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._db_path = Path(db_path)
        self._sources_path = Path(sources_path)
        self._models_dir = Path(models_dir) if models_dir else None
        self._public_fallback_url = public_fallback_url
        self._max_concurrent = max_concurrent_fetches
        self._proxy = proxy or None
        self._rsshub_port = rsshub_port
        self._autostart_rsshub = autostart_rsshub
        self._rsshub = rsshub
        self._rss_fetch = rss_fetch  # test seams
        self._rsshub_fetch = rsshub_fetch

    def run_impl(self) -> None:
        try:
            stored = asyncio.run(self._refresh())
            self.articles_stored.emit(stored)
            self.finished_ok.emit(len(stored))
        except Exception as exc:  # surface to UI, never crash silently
            logger.exception("RefreshWorker failed")
            self.failed.emit(str(exc))

    async def _refresh(self) -> list[Article]:
        sources = [s for s in load_sources(self._sources_path) if s.enabled]
        name_by_id = {s.id: s.name for s in sources}
        http_client = (
            HttpClient(proxy=self._proxy) if self._proxy else None
        )
        manager, rsshub = self._ensure_rsshub()
        try:
            orchestrator = FetchOrchestrator(
                rsshub=rsshub,
                public_fallback_url=self._public_fallback_url,
                max_concurrent_fetches=self._max_concurrent,
                http_client=http_client,
                rss_fetch=self._rss_fetch,
                rsshub_fetch=self._rsshub_fetch,
            )
            results = await orchestrator.fetch_all(sources)
        finally:
            if manager is not None:
                manager.stop()

        db = Database(self._db_path)  # thread-local connection
        db.ensure_schema()
        pipeline, region = _load_classifiers(self._models_dir)

        stored: list[Article] = []
        new_counts: dict[str, int] = {}
        for source_id, result in results.items():
            self.progress.emit(f"{source_id}: {'OK' if result.ok else '失败'}")
            for article in result.articles:
                classify_article(article, pipeline, region)
                inserted_id = insert_article(db, article)
                if inserted_id == article.id:  # dedup returns existing id
                    stored.append(article)
                    new_counts[source_id] = new_counts.get(source_id, 0) + 1

        self.refresh_report.emit(
            [
                {
                    "source_id": source_id,
                    "name": name_by_id.get(source_id, source_id),
                    "ok": result.ok,
                    "channel": result.channel,
                    "new": new_counts.get(source_id, 0),
                    "error": result.errors[-1] if result.errors else "",
                }
                for source_id, result in results.items()
            ]
        )
        return stored

    def _ensure_rsshub(self):
        """Start the embedded server unless a manager was injected.

        Returns ``(manager_or_None, rsshub_or_None)``; the caller stops the
        manager after fetching. Auto-start is opt-in so tests stay offline.
        """
        if self._rsshub is not None:
            return None, self._rsshub
        if not self._autostart_rsshub:
            return None, None
        from core.rsshub.manager import RSSHubManager

        manager = RSSHubManager(port=self._rsshub_port)
        try:
            if manager.start():
                return manager, manager
            logger.error("Embedded RSSHub failed to start; public fallback only")
        except Exception:
            logger.exception("Embedded RSSHub unavailable; public fallback only")
        return None, None


class QueryWorker(_ThreadWorker):
    """One read-only query per instance; replies once on results_ready."""

    results_ready = Signal(list)

    def __init__(
        self,
        *,
        db_path: Path,
        kind: str,
        category: str | None = None,
        region: str | None = None,
        query: str = "",
        article_id: str | None = None,
        limit: int = 200,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._db_path = Path(db_path)
        self._kind = kind
        self._category = category
        self._region = region
        self._query = query
        self._article_id = article_id
        self._limit = limit

    @classmethod
    def list_articles(cls, db_path: Path, *, category: str | None = None,
                      region: str | None = None, limit: int = 200) -> QueryWorker:
        """Browse mode with optional sidebar filters."""
        return cls(db_path=db_path, kind="list", category=category,
                   region=region, limit=limit)

    @classmethod
    def search(cls, db_path: Path, query: str, *, limit: int = 200) -> QueryWorker:
        """FTS5 search mode (P4.5)."""
        return cls(db_path=db_path, kind="search", query=query, limit=limit)

    @classmethod
    def detail(cls, db_path: Path, article_id: str) -> QueryWorker:
        """Single-article fetch for the detail pane (P4.4)."""
        return cls(db_path=db_path, kind="detail", article_id=article_id)

    def run_impl(self) -> None:
        try:
            db = Database(self._db_path)
            db.ensure_schema()  # fresh-install first query creates tables
            if self._kind == "list":
                rows = list_articles(db, category=self._category or None,
                                     region=self._region or None, limit=self._limit)
            elif self._kind == "search":
                rows = [] if not self._query.strip() else search_articles(
                    db, self._query, limit=self._limit)
            elif self._kind == "detail":
                one = get_article(db, self._article_id or "")
                rows = [one] if one is not None else []
            else:
                raise ValueError(f"unknown query kind {self._kind!r}")
            self.results_ready.emit(rows)
        except Exception:
            logger.exception("QueryWorker %s failed", self._kind)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _load_classifiers(
    models_dir: Path | None,
) -> tuple[ClassificationPipeline | None, RegionClassifier | None]:
    """Build model-backed classifiers when artifacts exist; else None."""
    if models_dir is None:
        return None, None
    cat_model = models_dir / "category_classifier_int8.onnx"
    tokenizer_dir = models_dir / "tokenizer"
    classifier: BertClassifier | None = None
    region: RegionClassifier | None = None
    try:
        if cat_model.is_file() and tokenizer_dir.is_dir():
            classifier = BertClassifier.from_files(cat_model, tokenizer_dir)
            region_model = models_dir / "region_classifier_int8.onnx"
            region_bert = (
                BertClassifier.from_files(region_model, tokenizer_dir, labels=REGIONS)
                if region_model.is_file()
                else None
            )
            region = RegionClassifier(model=region_bert)
    except Exception:
        logger.exception("Classifier load failed; keyword-only mode")
    return (
        ClassificationPipeline(classifier) if classifier is not None else None,
        region,
    )


class ImageWorker(_ThreadWorker):
    """Download an article's images into the local cache (request #4)."""

    image_ready = Signal(str, str, str)  # article_id, src_url, local_path
    all_done = Signal(str, list)

    def __init__(
        self,
        *,
        article_id: str,
        urls: list[str],
        proxy: str | None = None,
        cache_base: Path | None = None,
        client: HttpClient | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._article_id = article_id
        self._urls = list(urls)
        self._proxy = proxy or None
        self._cache_base = cache_base
        self._client = client

    def run_impl(self) -> None:
        async def job():
            client = self._client or HttpClient(
                proxy=self._proxy, timeout_seconds=15, max_retry=1
            )

            async def on_saved(url: str, path: Path) -> None:
                self.image_ready.emit(self._article_id, url, str(path))

            return await fetch_images_to_cache(
                self._urls, client=client, base=self._cache_base,
                on_saved=on_saved,
            )

        mapping = asyncio.run(job())
        self.all_done.emit(self._article_id, mapping)


class TranslateWorker(_ThreadWorker):
    """Translate one article's title+body to Chinese on request (#3)."""

    translated = Signal(str, str, str)  # article_id, title_zh, body_zh
    failed_sig = Signal(str, str)  # article_id, error

    def __init__(
        self,
        *,
        article_id: str,
        title: str,
        body: str,
        source_lang: str = "auto",
        proxy: str | None = None,
        client: HttpClient | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._article_id = article_id
        self._title = title
        self._body = body
        self._source_lang = source_lang
        self._proxy = proxy or None
        self._client = client

    def run_impl(self) -> None:
        async def one(text: str) -> str:
            client = self._client or HttpClient(
                proxy=self._proxy,
                timeout_seconds=translation._TRANSLATE_TIMEOUT_S,
                max_retry=0,
            )
            return await translation.translate_text(
                text, client=client, source_lang=self._source_lang
            )

        async def job():
            # Title and body race the providers independently.
            title_task = asyncio.create_task(one(self._title))
            body_task = asyncio.create_task(one(self._body))
            title_zh = await title_task
            try:
                body_zh = await body_task
            except Exception as exc:
                logger.warning("body translation failed (%s); title only", exc)
                body_zh = ""
            return title_zh, body_zh

        try:
            title_zh, body_zh = asyncio.run(job())
        except Exception as exc:
            logger.warning("translate %s failed: %s", self._article_id, exc)
            self.failed_sig.emit(self._article_id, str(exc))
            return
        self.translated.emit(self._article_id, title_zh, body_zh)


class TranslateTitlesWorker(_ThreadWorker):
    """Batch-translate list titles in one provider call (request: 一键翻译)."""

    item_translated = Signal(str, str)  # article_id, zh_title
    finished_count = Signal(int)

    def __init__(
        self,
        *,
        items: list[tuple[str, str]],
        proxy: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._items = list(items)
        self._proxy = proxy or None

    def run_impl(self) -> None:
        titles = [t for _, t in self._items]
        results = asyncio.run(
            translation.translate_titles_batch(titles)
        )
        count = 0
        for (article_id, original), zh in zip(self._items, results, strict=True):
            if zh and zh != original:
                self.item_translated.emit(article_id, zh)
                count += 1
        self.finished_count.emit(count)
