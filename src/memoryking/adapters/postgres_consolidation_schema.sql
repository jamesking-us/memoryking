CREATE TABLE IF NOT EXISTS consolidation_runs (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 principal TEXT NOT NULL, state TEXT NOT NULL, from_position BIGINT NOT NULL,
 to_position BIGINT NOT NULL, observations BIGINT NOT NULL, prompt_version TEXT NOT NULL,
 permit TEXT, provider TEXT, model TEXT, filed BIGINT NOT NULL, dropped TEXT NOT NULL,
 created BIGINT NOT NULL, closed BIGINT, PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS consolidation_runs_frontier ON consolidation_runs(tenant,project,state,to_position);
ALTER TABLE consolidation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE consolidation_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON consolidation_runs
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
CREATE TABLE IF NOT EXISTS unreviewed_tier (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, enabled BIGINT NOT NULL,
 decided_by TEXT NOT NULL, decided BIGINT NOT NULL, PRIMARY KEY(tenant,project));
ALTER TABLE unreviewed_tier ENABLE ROW LEVEL SECURITY;
ALTER TABLE unreviewed_tier FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON unreviewed_tier
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
