BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS source_trust (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, source TEXT NOT NULL,
 level TEXT NOT NULL, decided_by TEXT NOT NULL, decided INTEGER NOT NULL,
 PRIMARY KEY(tenant,project,source));
CREATE TABLE IF NOT EXISTS observation_holds (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 reason TEXT NOT NULL, held INTEGER NOT NULL, state TEXT NOT NULL,
 released_by TEXT, released INTEGER,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS observation_holds_open ON observation_holds(tenant,project,state,held);
PRAGMA user_version=12;
COMMIT;
