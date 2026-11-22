BEGIN IMMEDIATE;
-- Operation records move to their own log; the database tracks how far it has applied that log.
-- The instance lease records which process family holds a tenant (see docs/deployment.md).
-- The events.on_behalf_of column is added by the loader, which checks for it first (ALTER is not
-- repeatable); it names the subject a delegated credential acted for and is null otherwise.
INSERT OR IGNORE INTO meta(key,value) VALUES ('operations','0');
CREATE TABLE IF NOT EXISTS instance_lease (
 tenant TEXT NOT NULL, holder TEXT NOT NULL, renewed_ms INTEGER NOT NULL,
 PRIMARY KEY(tenant));
PRAGMA user_version=18;
COMMIT;
