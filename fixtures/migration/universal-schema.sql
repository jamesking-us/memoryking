-- Selected actual table DDL from Loreweaver d7e5a7b; synthetic rows only.
CREATE TABLE IF NOT EXISTS lw_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL
                );

CREATE TABLE lw_scopes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_scope_id INTEGER REFERENCES lw_scopes(id),
                kind TEXT NOT NULL CHECK(kind IN ('workspace','project','run')),
                external_key TEXT NOT NULL,
                name TEXT NOT NULL,
                lifecycle TEXT NOT NULL DEFAULT 'active'
                    CHECK(lifecycle IN ('active','frozen','archived','deleted')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

CREATE TABLE lw_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL REFERENCES lw_scopes(id),
                kind TEXT NOT NULL CHECK(kind IN (
                    'operator_statement','plan_revision','run_event','artifact','test_result',
                    'review','incident','deployment')),
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_uri TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                trust_level TEXT NOT NULL DEFAULT 'observed'
                    CHECK(trust_level IN ('untrusted','observed','verified','operator')),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                observed_at TEXT,
                recorded_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                UNIQUE(scope_id, content_hash)
            );

CREATE TABLE lw_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL REFERENCES lw_scopes(id),
                memory_key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN (
                    'semantic','episodic','procedural','causal','decision','risk','outcome')),
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                lifecycle TEXT NOT NULL DEFAULT 'candidate' CHECK(lifecycle IN (
                    'candidate','reviewed','verified','generalized','superseded','deprecated',
                    'archived','deleted')),
                confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence >= 0 AND confidence <= 1),
                valid_from TEXT,
                valid_to TEXT,
                recorded_at TEXT NOT NULL,
                retired_at TEXT,
                created_by TEXT NOT NULL DEFAULT '',
                source_system TEXT NOT NULL DEFAULT '',
                supersedes_memory_id INTEGER REFERENCES lw_memories(id),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                embedding BLOB,
                embedding_provider TEXT NOT NULL DEFAULT '',
                UNIQUE(scope_id, memory_key, revision)
            );

CREATE TABLE lw_memory_evidence (
                memory_id INTEGER NOT NULL REFERENCES lw_memories(id) ON DELETE CASCADE,
                evidence_id INTEGER NOT NULL REFERENCES lw_evidence(id),
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(memory_id, evidence_id)
            );

CREATE TABLE lw_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_type TEXT NOT NULL,
                subject_id INTEGER NOT NULL,
                relation TEXT NOT NULL CHECK(relation IN (
                    'supports','contradicts','supersedes','caused_by','applied_to','derived_from',
                    'produced','invalidated_by')),
                object_type TEXT NOT NULL,
                object_id INTEGER NOT NULL,
                evidence_id INTEGER REFERENCES lw_evidence(id),
                attributes_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                UNIQUE(subject_type, subject_id, relation, object_type, object_id)
            );

CREATE TABLE lw_procedures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL REFERENCES lw_scopes(id),
                procedure_key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('directive','playbook','checklist','template','recovery')),
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                applicability_json TEXT NOT NULL,
                tests_json TEXT NOT NULL,
                lifecycle TEXT NOT NULL DEFAULT 'draft' CHECK(lifecycle IN
                    ('draft','reviewed','active','suspended','superseded','deprecated','archived')),
                conflict_group TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 50 CHECK(priority >= 0 AND priority <= 100),
                supersedes_procedure_id INTEGER REFERENCES lw_procedures(id),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT,
                reviewed_by TEXT NOT NULL DEFAULT '',
                review_notes TEXT NOT NULL DEFAULT '',
                activated_at TEXT,
                activated_by TEXT NOT NULL DEFAULT '',
                retired_at TEXT,
                UNIQUE(scope_id,procedure_key,revision)
            );

CREATE TABLE lw_procedure_evidence (
                procedure_id INTEGER NOT NULL REFERENCES lw_procedures(id) ON DELETE CASCADE,
                evidence_id INTEGER NOT NULL REFERENCES lw_evidence(id),
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(procedure_id,evidence_id)
            );
