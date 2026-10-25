BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS checkpoint_streams (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, task TEXT NOT NULL,
 stream TEXT NOT NULL, writer TEXT NOT NULL, revision INTEGER NOT NULL,
 incarnation TEXT NOT NULL, latest TEXT,
 PRIMARY KEY(tenant,project,task,stream));
CREATE TABLE IF NOT EXISTS checkpoints (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 task TEXT NOT NULL, stream TEXT NOT NULL, revision INTEGER NOT NULL,
 writer TEXT NOT NULL, incarnation TEXT NOT NULL, created INTEGER NOT NULL,
 body TEXT, body_hash TEXT NOT NULL, readers TEXT NOT NULL, dependencies TEXT NOT NULL,
 state TEXT NOT NULL, position INTEGER NOT NULL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,project,task,stream,revision));
CREATE TABLE IF NOT EXISTS operation_intents (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, principal TEXT NOT NULL,
 project TEXT NOT NULL, task TEXT NOT NULL, stream TEXT NOT NULL,
 incarnation TEXT NOT NULL, target_hash TEXT NOT NULL, parameters_hash TEXT NOT NULL,
 method TEXT NOT NULL, provider_key TEXT, state TEXT NOT NULL, revision INTEGER NOT NULL,
 result_hash TEXT, evidence TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL,
 reconciled_incarnation TEXT,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS operations_scope ON operation_intents(tenant,project,task,stream);
PRAGMA user_version=4;
COMMIT;
