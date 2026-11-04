BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS session_summaries (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 task TEXT NOT NULL, stream TEXT NOT NULL, checkpoint TEXT NOT NULL, revision INTEGER NOT NULL,
 principal TEXT NOT NULL, incarnation TEXT NOT NULL, method TEXT NOT NULL, text TEXT NOT NULL,
 readers TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS session_summaries_checkpoint ON session_summaries(tenant,checkpoint,created);
PRAGMA user_version=11;
COMMIT;
