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

from PySide6.QtCore import QThread, Signal

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


class RefreshWorker(QThread):
    """Fetch every enabled source, classify and store; fully off-UI."""

    progress = Signal(str)
    articles_stored = Signal(list)
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
        self._rss_fetch = rss_fetch  # test seams
        self._rsshub_fetch = rsshub_fetch

    def run(self) -> None:  # noqa: D102 - QThread override
        try:
            stored = asyncio.run(self._refresh())
            self.articles_stored.emit(stored)
            self.finished_ok.emit(len(stored))
        except Exception as exc:  # surface to UI, never crash silently
            logger.exception("RefreshWorker failed")
            self.failed.emit(str(exc))

    async def _refresh(self) -> list[Article]:
        sources = [s for s in load_sources(self._sources_path) if s.enabled]
        orchestrator = FetchOrchestrator(
            public_fallback_url=self._public_fallback_url,
            max_concurrent_fetches=self._max_concurrent,
            rss_fetch=self._rss_fetch,
            rsshub_fetch=self._rsshub_fetch,
        )
        results = await orchestrator.fetch_all(sources)

        db = Database(self._db_path)  # thread-local connection
        db.initialize()
        pipeline, region = _load_classifiers(self._models_dir)

        stored: list[Article] = []
        for result in results.values():
            self.progress.emit(f"{result.source_id}: {'OK' if result.ok else '失败'}")
            for article in result.articles:
                classify_article(article, pipeline, region)
                inserted_id = insert_article(db, article)
                if inserted_id == article.id:  # dedup returns existing id
                    stored.append(article)
        return stored


class QueryWorker(QThread):
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

    def run(self) -> None:  # noqa: D102 - QThread override
        try:
            db = Database(self._db_path)
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
