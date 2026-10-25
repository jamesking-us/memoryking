-- Final SQLite schema produced by KingForge's Python Loreweaver server.
-- Source: kingforge/mcp/loreweaver/server.py, frozen 2026-09-19.
CREATE TABLE plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    seq INTEGER NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo',
    notes TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);

CREATE TABLE lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER REFERENCES plans(id),
    title TEXT NOT NULL,
    situation TEXT NOT NULL,
    lesson TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'general',
    tags TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE VIRTUAL TABLE lessons_fts USING fts5(
    title, situation, lesson, tags, content='lessons', content_rowid='id'
);

CREATE TRIGGER lessons_ai AFTER INSERT ON lessons BEGIN
    INSERT INTO lessons_fts(rowid, title, situation, lesson, tags)
    VALUES (new.id, new.title, new.situation, new.lesson, new.tags);
END;

-- Additive migration performed by _ensure_embedding_columns.
ALTER TABLE plans ADD COLUMN embedding BLOB;
ALTER TABLE lessons ADD COLUMN embedding BLOB;

