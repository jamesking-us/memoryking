CREATE TABLE IF NOT EXISTS session_summaries (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 task TEXT NOT NULL, stream TEXT NOT NULL, checkpoint TEXT NOT NULL, revision BIGINT NOT NULL,
 principal TEXT NOT NULL, incarnation TEXT NOT NULL, method TEXT NOT NULL, text TEXT NOT NULL,
 readers TEXT NOT NULL, created BIGINT NOT NULL, expires BIGINT NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS session_summaries_checkpoint ON session_summaries(tenant,checkpoint,created);
ALTER TABLE session_summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_summaries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON session_summaries
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
