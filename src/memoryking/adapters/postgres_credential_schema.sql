CREATE TABLE IF NOT EXISTS credential_generations (
 tenant TEXT NOT NULL REFERENCES tenants, principal TEXT NOT NULL, generation BIGINT NOT NULL,
 PRIMARY KEY(tenant,principal));
ALTER TABLE credential_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE credential_generations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON credential_generations
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
