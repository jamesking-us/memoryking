CREATE TABLE IF NOT EXISTS source_trust (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, source TEXT NOT NULL,
 level TEXT NOT NULL, decided_by TEXT NOT NULL, decided BIGINT NOT NULL,
 PRIMARY KEY(tenant,project,source));
ALTER TABLE source_trust ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_trust FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON source_trust
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
CREATE TABLE IF NOT EXISTS observation_holds (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 reason TEXT NOT NULL, held BIGINT NOT NULL, state TEXT NOT NULL,
 released_by TEXT, released BIGINT,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS observation_holds_open ON observation_holds(tenant,project,state,held);
ALTER TABLE observation_holds ENABLE ROW LEVEL SECURITY;
ALTER TABLE observation_holds FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON observation_holds
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
