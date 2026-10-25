-- Native migration 2 -> 3: reviewed experience and frozen ranking decisions.
CREATE TABLE experience_records (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 principal TEXT NOT NULL, incarnation TEXT NOT NULL, kind TEXT NOT NULL,
 logical_key TEXT NOT NULL, revision BIGINT NOT NULL, body TEXT,
 body_hash TEXT NOT NULL, dependencies TEXT NOT NULL, readers TEXT,
 position BIGINT NOT NULL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,project,principal,incarnation,kind,logical_key,revision),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position));
CREATE INDEX experience_lookup ON experience_records(tenant,project,kind,position);
CREATE TABLE ranking_state (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, version BIGINT NOT NULL,
 configuration TEXT, reconciliation BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,project),
 FOREIGN KEY(tenant,configuration) REFERENCES experience_records(tenant,id));
CREATE TABLE ranking_history (
 tenant TEXT NOT NULL, project TEXT NOT NULL, position BIGINT NOT NULL,
 configuration TEXT, detail TEXT NOT NULL,
 PRIMARY KEY(tenant,project,position),
 FOREIGN KEY(tenant,configuration) REFERENCES experience_records(tenant,id),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position));

ALTER TABLE experience_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE experience_records FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON experience_records
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE ranking_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE ranking_state FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON ranking_state
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE ranking_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE ranking_history FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON ranking_history
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
