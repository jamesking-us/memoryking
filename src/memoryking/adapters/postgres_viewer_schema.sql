CREATE TABLE IF NOT EXISTS viewer_registries (
 tenant TEXT NOT NULL REFERENCES tenants, installation TEXT NOT NULL, delegator TEXT NOT NULL,
 project TEXT NOT NULL, provider TEXT NOT NULL, audience TEXT NOT NULL, revision BIGINT NOT NULL,
 enabled BIGINT NOT NULL, starts BIGINT, expires BIGINT,
 PRIMARY KEY(tenant,installation,delegator,project));
CREATE INDEX IF NOT EXISTS viewer_delegators ON viewer_registries(tenant,delegator);
CREATE TABLE IF NOT EXISTS viewer_mappings (
 tenant TEXT NOT NULL REFERENCES tenants, installation TEXT NOT NULL, provider TEXT NOT NULL,
 github_id TEXT NOT NULL, subject TEXT NOT NULL, revision BIGINT NOT NULL,
 enabled BIGINT NOT NULL, starts BIGINT, expires BIGINT,
 PRIMARY KEY(tenant,installation,provider,github_id));
CREATE INDEX IF NOT EXISTS viewer_subjects ON viewer_mappings(tenant,subject);
-- Ephemeral leases are not journal records; incarnation/generations are checked on every use.
CREATE TABLE IF NOT EXISTS viewer_leases (
 tenant TEXT NOT NULL REFERENCES tenants, lease_hash TEXT NOT NULL, delegator TEXT NOT NULL,
 subject TEXT NOT NULL, session_ref TEXT NOT NULL, expires BIGINT NOT NULL,
 closed BIGINT NOT NULL DEFAULT 0, body TEXT NOT NULL, PRIMARY KEY(tenant,lease_hash));
CREATE INDEX IF NOT EXISTS viewer_live ON viewer_leases(tenant,expires);
CREATE INDEX IF NOT EXISTS viewer_backend_live ON viewer_leases(tenant,delegator,closed,expires);
CREATE INDEX IF NOT EXISTS viewer_session_live ON viewer_leases(tenant,subject,session_ref,closed,expires);
CREATE TABLE IF NOT EXISTS viewer_receipt_context (
 tenant TEXT NOT NULL, receipt TEXT NOT NULL, context TEXT NOT NULL, PRIMARY KEY(tenant,receipt),
 FOREIGN KEY(tenant,receipt) REFERENCES receipts(tenant,id) ON DELETE CASCADE);
ALTER TABLE viewer_registries ENABLE ROW LEVEL SECURITY;
ALTER TABLE viewer_registries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON viewer_registries
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
ALTER TABLE viewer_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE viewer_mappings FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON viewer_mappings
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
ALTER TABLE viewer_leases ENABLE ROW LEVEL SECURITY;
ALTER TABLE viewer_leases FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON viewer_leases
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
ALTER TABLE viewer_receipt_context ENABLE ROW LEVEL SECURITY;
ALTER TABLE viewer_receipt_context FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON viewer_receipt_context
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
