BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS rendered_artifacts (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, principal TEXT NOT NULL,
 project TEXT NOT NULL, incarnation TEXT NOT NULL, created INTEGER NOT NULL,
 expires INTEGER NOT NULL, state TEXT NOT NULL, body BLOB, body_hash TEXT NOT NULL,
 request_json TEXT, diagnostics TEXT, dependencies TEXT NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS rendered_owner ON rendered_artifacts(tenant,principal,project,created);
PRAGMA user_version=3;
COMMIT;
