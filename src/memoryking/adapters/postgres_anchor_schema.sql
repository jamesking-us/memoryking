CREATE TABLE IF NOT EXISTS anchors (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, anchor TEXT NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(tenant,id,anchor));
CREATE INDEX IF NOT EXISTS anchors_lookup ON anchors(tenant,anchor,id);
ALTER TABLE anchors ENABLE ROW LEVEL SECURITY;
ALTER TABLE anchors FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON anchors
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
