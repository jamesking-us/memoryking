BEGIN IMMEDIATE;
CREATE TABLE experience_records (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 principal TEXT NOT NULL, incarnation TEXT NOT NULL, kind TEXT NOT NULL,
 logical_key TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT,
 body_hash TEXT NOT NULL, dependencies TEXT NOT NULL, readers TEXT,
 position INTEGER NOT NULL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,project,principal,incarnation,kind,logical_key,revision),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position));
CREATE INDEX experience_lookup ON experience_records(tenant,project,kind,position);
CREATE TABLE ranking_state (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, version INTEGER NOT NULL,
 configuration TEXT, reconciliation INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,project),
 FOREIGN KEY(tenant,configuration) REFERENCES experience_records(tenant,id));
CREATE TABLE ranking_history (
 tenant TEXT NOT NULL, project TEXT NOT NULL, position INTEGER NOT NULL,
 configuration TEXT, detail TEXT NOT NULL,
 PRIMARY KEY(tenant,project,position),
 FOREIGN KEY(tenant,configuration) REFERENCES experience_records(tenant,id),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position));
PRAGMA user_version=8;
COMMIT;
