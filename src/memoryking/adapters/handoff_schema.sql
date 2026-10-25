BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS handoffs (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 task TEXT NOT NULL, stream TEXT NOT NULL, checkpoint TEXT NOT NULL,
 sender TEXT NOT NULL, recipient TEXT NOT NULL, role TEXT NOT NULL, stage TEXT NOT NULL,
 bindings TEXT NOT NULL, incarnation TEXT NOT NULL, created INTEGER NOT NULL,
 expires INTEGER NOT NULL, state TEXT NOT NULL, stream_revision INTEGER NOT NULL,
 ack_view TEXT,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS handoff_scope ON handoffs(tenant,project,task,stream,sender,recipient);
CREATE TABLE IF NOT EXISTS handoff_views (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, handoff TEXT NOT NULL,
 actor TEXT NOT NULL, mode TEXT NOT NULL, stamp TEXT NOT NULL, body_hash TEXT NOT NULL,
 status TEXT NOT NULL, created INTEGER NOT NULL,
 PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,handoff) REFERENCES handoffs(tenant,id));
PRAGMA user_version=5;
COMMIT;
