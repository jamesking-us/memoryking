-- Operation records move to their own log; the database tracks how far it has applied that log.
-- The instance lease records which process family holds a tenant (see docs/deployment.md).
-- on_behalf_of names the subject a delegated credential acted for; the request's transaction
-- supplies it as a setting, so every event and receipt the request writes carries it and every
-- other write leaves it null.
INSERT INTO tenant_meta SELECT tenant,'operations','0' FROM tenants ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS instance_lease (
 tenant TEXT NOT NULL REFERENCES tenants, holder TEXT NOT NULL, renewed_ms BIGINT NOT NULL,
 PRIMARY KEY(tenant));
ALTER TABLE instance_lease ENABLE ROW LEVEL SECURITY;
ALTER TABLE instance_lease FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON instance_lease
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
ALTER TABLE events ADD COLUMN on_behalf_of TEXT;
ALTER TABLE events ALTER COLUMN on_behalf_of SET DEFAULT NULLIF(current_setting('memoryking.on_behalf_of',true),'');
ALTER TABLE receipts ADD COLUMN on_behalf_of TEXT;
ALTER TABLE receipts ALTER COLUMN on_behalf_of SET DEFAULT NULLIF(current_setting('memoryking.on_behalf_of',true),'');
