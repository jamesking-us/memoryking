CREATE TABLE IF NOT EXISTS embedding_vectors (
 tenant TEXT NOT NULL, project TEXT NOT NULL, fingerprint TEXT NOT NULL, generation BIGINT NOT NULL,
 kind TEXT NOT NULL, subject TEXT NOT NULL, part BIGINT NOT NULL,
 incarnation TEXT NOT NULL, source_revision BIGINT NOT NULL, blob_hash TEXT,
 input_hash TEXT NOT NULL, vector BYTEA NOT NULL,
 start_byte BIGINT NOT NULL, end_byte BIGINT NOT NULL, input_tokens BIGINT,
 truncated BIGINT NOT NULL, position BIGINT NOT NULL,
 PRIMARY KEY(tenant,project,fingerprint,generation,kind,subject,part),
 FOREIGN KEY(tenant,project,fingerprint) REFERENCES embedding_partitions(tenant,project,fingerprint));
CREATE INDEX IF NOT EXISTS embedding_vectors_subject ON embedding_vectors(tenant,kind,subject);
CREATE INDEX IF NOT EXISTS embedding_vectors_recent ON embedding_vectors(tenant,project,fingerprint,generation,kind,position);
ALTER TABLE embedding_vectors ENABLE ROW LEVEL SECURITY;
ALTER TABLE embedding_vectors FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON embedding_vectors
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
CREATE TABLE IF NOT EXISTS embedding_skips (
 tenant TEXT NOT NULL, fingerprint TEXT NOT NULL, generation BIGINT NOT NULL, subject TEXT NOT NULL,
 PRIMARY KEY(tenant,fingerprint,generation,subject));
ALTER TABLE embedding_skips ENABLE ROW LEVEL SECURITY;
ALTER TABLE embedding_skips FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON embedding_skips
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
