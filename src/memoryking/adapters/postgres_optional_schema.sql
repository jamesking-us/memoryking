-- Native PostgreSQL shared migration 1 -> 2; no pgvector dependency.
CREATE TABLE embedding_partitions (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, fingerprint TEXT NOT NULL,
 incarnation TEXT NOT NULL, generation BIGINT NOT NULL, dimensions BIGINT NOT NULL CHECK(dimensions BETWEEN 1 AND 4096),
 configuration TEXT NOT NULL, state TEXT NOT NULL, scheduled BIGINT NOT NULL DEFAULT 0,
 frontier BIGINT NOT NULL DEFAULT 0, created BIGINT NOT NULL,
 PRIMARY KEY(tenant,project,fingerprint));
CREATE TABLE embedding_jobs (
 tenant TEXT NOT NULL, project TEXT NOT NULL, fingerprint TEXT NOT NULL, generation BIGINT NOT NULL,
 position BIGINT NOT NULL, observation TEXT, source_revision BIGINT NOT NULL, blob_hash TEXT,
 incarnation TEXT NOT NULL, state TEXT NOT NULL, attempts BIGINT NOT NULL DEFAULT 0,
 fence BIGINT NOT NULL DEFAULT 0, lease_until BIGINT NOT NULL DEFAULT 0,
 available BIGINT NOT NULL DEFAULT 0, principal TEXT, purge_epoch BIGINT NOT NULL,
 PRIMARY KEY(tenant,project,fingerprint,generation,position),
 FOREIGN KEY(tenant,project,fingerprint) REFERENCES embedding_partitions(tenant,project,fingerprint),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position),
 FOREIGN KEY(tenant,observation) REFERENCES observations(tenant,id));
CREATE INDEX embedding_queue ON embedding_jobs(tenant,project,fingerprint,state,position);
CREATE TABLE embedding_items (
 tenant TEXT NOT NULL, project TEXT NOT NULL, fingerprint TEXT NOT NULL, generation BIGINT NOT NULL,
 observation TEXT NOT NULL, incarnation TEXT NOT NULL, source_revision BIGINT NOT NULL,
 blob_hash TEXT NOT NULL, input_hash TEXT NOT NULL, vector_json TEXT NOT NULL,
 start_byte BIGINT NOT NULL, end_byte BIGINT NOT NULL, input_tokens BIGINT,
 truncated BIGINT NOT NULL, position BIGINT NOT NULL,
 PRIMARY KEY(tenant,project,fingerprint,generation,observation),
 FOREIGN KEY(tenant,project,fingerprint) REFERENCES embedding_partitions(tenant,project,fingerprint),
 FOREIGN KEY(tenant,observation) REFERENCES observations(tenant,id));
CREATE TABLE processing_usage (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 principal TEXT NOT NULL, incarnation TEXT NOT NULL, provider TEXT NOT NULL,
 fingerprint TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
 input_tokens BIGINT, cached_input_tokens BIGINT, output_tokens BIGINT,
 cost_microusd BIGINT, duration_ms BIGINT NOT NULL, request_hash TEXT NOT NULL,
 output_hash TEXT, dependencies TEXT NOT NULL, created BIGINT NOT NULL, expires BIGINT NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE TABLE adapter_bases (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 principal TEXT NOT NULL, incarnation TEXT NOT NULL, binding TEXT NOT NULL,
 body BYTEA NOT NULL, body_hash TEXT NOT NULL, dependencies TEXT NOT NULL,
 created BIGINT NOT NULL, expires BIGINT NOT NULL,
 PRIMARY KEY(tenant,id));

ALTER TABLE embedding_partitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE embedding_partitions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON embedding_partitions
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE embedding_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE embedding_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON embedding_jobs
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE embedding_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE embedding_items FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON embedding_items
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE processing_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE processing_usage FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON processing_usage
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE adapter_bases ENABLE ROW LEVEL SECURITY;
ALTER TABLE adapter_bases FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON adapter_bases
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
