CREATE TABLE IF NOT EXISTS lexical_state (
 tenant TEXT NOT NULL REFERENCES tenants, state TEXT NOT NULL, cursor BIGINT NOT NULL DEFAULT 0,
 configuration TEXT NOT NULL, PRIMARY KEY(tenant));
CREATE TABLE IF NOT EXISTS lexical_projects (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, documents BIGINT NOT NULL,
 tokens BIGINT NOT NULL, PRIMARY KEY(tenant,project));
CREATE TABLE IF NOT EXISTS lexical_terms (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, term TEXT NOT NULL,
 documents BIGINT NOT NULL, PRIMARY KEY(tenant,project,term));
ALTER TABLE lexical_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE lexical_state FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lexical_state
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
ALTER TABLE lexical_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE lexical_projects FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lexical_projects
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
ALTER TABLE lexical_terms ENABLE ROW LEVEL SECURITY;
ALTER TABLE lexical_terms FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lexical_terms
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
CREATE INDEX IF NOT EXISTS observations_position ON observations(tenant,position);
