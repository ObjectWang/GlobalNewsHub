-- 002: switch the FTS5 index to the trigram tokenizer.
--
-- Why (deviation from section 2.3 DDL, recorded in AI_DEVELOPMENT_PLAN.md):
-- the default unicode61 tokenizer treats a contiguous CJK run as ONE
-- token, so "芯片" can never match title "芯片出口新规" — Chinese search
-- (a hard product requirement, section 1.3 offline search) would only
-- work for exact whole-run matches. trigram enables substring MATCH for
-- queries of >= 3 characters. Queries shorter than 3 characters fall
-- back to LIKE in core.storage.crud.search_articles.
--
-- Re-running this script rebuilds the index from the content table,
-- so it stays idempotent.

DROP TABLE IF EXISTS articles_fts;

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, summary, content,
    content='articles',
    content_rowid='rowid',
    tokenize='trigram'
);

INSERT INTO articles_fts(articles_fts) VALUES ('rebuild');
