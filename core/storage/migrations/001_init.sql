-- migrations/001_init.sql
CREATE TABLE IF NOT EXISTS articles (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    summary       TEXT,
    content       TEXT,
    source_media  TEXT NOT NULL,
    source_url    TEXT NOT NULL UNIQUE,
    category      TEXT NOT NULL DEFAULT 'other',
    region        TEXT NOT NULL DEFAULT 'unknown',
    published_at  TEXT,
    fetched_at    TEXT NOT NULL,
    tags          TEXT,
    language      TEXT DEFAULT 'zh-CN',
    word_count    INTEGER DEFAULT 0,
    image_urls    TEXT,
    is_paywalled  INTEGER DEFAULT 0,
    user_feedback TEXT,
    is_favorite   INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_category  ON articles(category);
CREATE INDEX IF NOT EXISTS idx_region    ON articles(region);
CREATE INDEX IF NOT EXISTS idx_published ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_source    ON articles(source_media);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, summary, content,
    content='articles',
    content_rowid='rowid'
);

CREATE TABLE IF NOT EXISTS source_health (
    source_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    last_status   TEXT,
    fail_count    INTEGER DEFAULT 0,
    last_fetch_at TEXT,
    disabled      INTEGER DEFAULT 0
);
