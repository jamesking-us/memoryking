CREATE TABLE IF NOT EXISTS supersessions (
 tenant TEXT NOT NULL REFERENCES tenants, replacement TEXT NOT NULL, superseded TEXT NOT NULL,
 project TEXT NOT NULL, principal TEXT NOT NULL, recorded BIGINT NOT NULL,
 PRIMARY KEY(tenant,replacement));
CREATE INDEX IF NOT EXISTS supersessions_old ON supersessions(tenant,superseded);
CREATE TABLE IF NOT EXISTS retirement_requests (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 observation TEXT NOT NULL, requester TEXT NOT NULL, note TEXT NOT NULL, state TEXT NOT NULL,
 requested BIGINT NOT NULL, decided_by TEXT, decided BIGINT, PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS retirement_requests_open ON retirement_requests(tenant,project,state,requested);
CREATE INDEX IF NOT EXISTS retirement_requests_record ON retirement_requests(tenant,observation);
CREATE TABLE IF NOT EXISTS pins (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, kind TEXT NOT NULL,
 target TEXT NOT NULL, pinned_by TEXT NOT NULL, pinned BIGINT NOT NULL,
 PRIMARY KEY(tenant,project,kind,target));
ALTER TABLE supersessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE supersessions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON supersessions
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
ALTER TABLE retirement_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE retirement_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON retirement_requests
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
ALTER TABLE pins ENABLE ROW LEVEL SECURITY;
ALTER TABLE pins FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON pins
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
