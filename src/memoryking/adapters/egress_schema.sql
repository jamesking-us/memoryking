BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS egress_audit (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, principal TEXT NOT NULL,
 project TEXT NOT NULL, incarnation TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 host TEXT NOT NULL, policy_revision TEXT NOT NULL, request_hash TEXT NOT NULL,
 manifest_hash TEXT NOT NULL, manifest TEXT NOT NULL, classification TEXT NOT NULL,
 request_bytes INTEGER NOT NULL, evaluated INTEGER NOT NULL, deadline INTEGER NOT NULL,
 status TEXT NOT NULL, wire_hash TEXT, response_hash TEXT, input_tokens INTEGER,
 output_tokens INTEGER, reported INTEGER, expires INTEGER NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS egress_audit_project ON egress_audit(tenant,project,evaluated);
PRAGMA user_version=10;
COMMIT;
