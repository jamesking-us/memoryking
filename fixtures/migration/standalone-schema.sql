-- Actual ordinary-table DDL from Loreweaver d7e5a7b; synthetic rows only.
CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                project TEXT NOT NULL DEFAULT '',
                feature TEXT NOT NULL,
                title TEXT NOT NULL,
                path TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                version INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                heading TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                embedding BLOB,
                embedding_provider TEXT NOT NULL DEFAULT ''
            );

CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'general',
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                source_feature TEXT NOT NULL DEFAULT '',
                embedding BLOB,
                embedding_provider TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
