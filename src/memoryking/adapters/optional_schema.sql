BEGIN IMMEDIATE;
CREATE TABLE embedding_partitions (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, fingerprint TEXT NOT NULL,
 incarnation TEXT NOT NULL, generation INTEGER NOT NULL, dimensions INTEGER NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
 configuration TEXT NOT NULL, state TEXT NOT NULL, scheduled INTEGER NOT NULL DEFAULT 0,
 frontier INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL,
 PRIMARY KEY(tenant,project,fingerprint));
CREATE TABLE embedding_jobs (
 tenant TEXT NOT NULL, project TEXT NOT NULL, fingerprint TEXT NOT NULL, generation INTEGER NOT NULL,
 position INTEGER NOT NULL, observation TEXT, source_revision INTEGER NOT NULL, blob_hash TEXT,
 incarnation TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 fence INTEGER NOT NULL DEFAULT 0, lease_until INTEGER NOT NULL DEFAULT 0,
 available INTEGER NOT NULL DEFAULT 0, principal TEXT, purge_epoch INTEGER NOT NULL,
 PRIMARY KEY(tenant,project,fingerprint,generation,position),
 FOREIGN KEY(tenant,project,fingerprint) REFERENCES embedding_partitions(tenant,project,fingerprint),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position),
 FOREIGN KEY(tenant,observation) REFERENCES observations(tenant,id));
CREATE INDEX embedding_queue ON embedding_jobs(tenant,project,fingerprint,state,position);
CREATE TABLE embedding_items (
 tenant TEXT NOT NULL, project TEXT NOT NULL, fingerprint TEXT NOT NULL, generation INTEGER NOT NULL,
 observation TEXT NOT NULL, incarnation TEXT NOT NULL, source_revision INTEGER NOT NULL,
 blob_hash TEXT NOT NULL, input_hash TEXT NOT NULL, vector_json TEXT NOT NULL,
 start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL, input_tokens INTEGER,
 truncated INTEGER NOT NULL, position INTEGER NOT NULL,
 PRIMARY KEY(tenant,project,fingerprint,generation,observation),
 FOREIGN KEY(tenant,project,fingerprint) REFERENCES embedding_partitions(tenant,project,fingerprint),
 FOREIGN KEY(tenant,observation) REFERENCES observations(tenant,id));
CREATE TABLE processing_usage (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 principal TEXT NOT NULL, incarnation TEXT NOT NULL, provider TEXT NOT NULL,
 fingerprint TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
 input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
 cost_microusd INTEGER, duration_ms INTEGER NOT NULL, request_hash TEXT NOT NULL,
 output_hash TEXT, dependencies TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE TABLE adapter_bases (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 principal TEXT NOT NULL, incarnation TEXT NOT NULL, binding TEXT NOT NULL,
 body BLOB NOT NULL, body_hash TEXT NOT NULL, dependencies TEXT NOT NULL,
 created INTEGER NOT NULL, expires INTEGER NOT NULL,
 PRIMARY KEY(tenant,id));
PRAGMA user_version=7;
COMMIT;
