CREATE TABLE IF NOT EXISTS egress_audit (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, principal TEXT NOT NULL,
 project TEXT NOT NULL, incarnation TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 host TEXT NOT NULL, policy_revision TEXT NOT NULL, request_hash TEXT NOT NULL,
 manifest_hash TEXT NOT NULL, manifest TEXT NOT NULL, classification TEXT NOT NULL,
 request_bytes BIGINT NOT NULL, evaluated BIGINT NOT NULL, deadline BIGINT NOT NULL,
 status TEXT NOT NULL, wire_hash TEXT, response_hash TEXT, input_tokens BIGINT,
 output_tokens BIGINT, reported BIGINT, expires BIGINT NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS egress_audit_project ON egress_audit(tenant,project,evaluated);
ALTER TABLE egress_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE egress_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON egress_audit
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
