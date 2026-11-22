BEGIN IMMEDIATE;
-- Embedding vectors in a compact binary form (16-bit integers, little endian, one unit-length
-- vector per row). `kind` is the kind of thing embedded (an observation or an accepted claim
-- revision) and `part` which piece of it (the passage number; 0 for the whole). Rows written
-- by earlier releases stay in embedding_items as JSON until the indexing loop converts them.
CREATE TABLE IF NOT EXISTS embedding_vectors (
 tenant TEXT NOT NULL, project TEXT NOT NULL, fingerprint TEXT NOT NULL, generation INTEGER NOT NULL,
 kind TEXT NOT NULL, subject TEXT NOT NULL, part INTEGER NOT NULL,
 incarnation TEXT NOT NULL, source_revision INTEGER NOT NULL, blob_hash TEXT,
 input_hash TEXT NOT NULL, vector BLOB NOT NULL,
 start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL, input_tokens INTEGER,
 truncated INTEGER NOT NULL, position INTEGER NOT NULL,
 PRIMARY KEY(tenant,project,fingerprint,generation,kind,subject,part),
 FOREIGN KEY(tenant,project,fingerprint) REFERENCES embedding_partitions(tenant,project,fingerprint));
CREATE INDEX IF NOT EXISTS embedding_vectors_subject ON embedding_vectors(tenant,kind,subject);
CREATE INDEX IF NOT EXISTS embedding_vectors_recent ON embedding_vectors(tenant,project,fingerprint,generation,kind,position);
-- Claim revisions that could not be embedded in a partition's generation, so that the
-- indexing loop does not offer them again.
CREATE TABLE IF NOT EXISTS embedding_skips (
 tenant TEXT NOT NULL, fingerprint TEXT NOT NULL, generation INTEGER NOT NULL, subject TEXT NOT NULL,
 PRIMARY KEY(tenant,fingerprint,generation,subject));
PRAGMA user_version=21;
COMMIT;
