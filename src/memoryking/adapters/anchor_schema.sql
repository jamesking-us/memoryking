BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS anchors (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, anchor TEXT NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(tenant,id,anchor));
CREATE INDEX IF NOT EXISTS anchors_lookup ON anchors(tenant,anchor,id);
PRAGMA user_version=13;
COMMIT;
