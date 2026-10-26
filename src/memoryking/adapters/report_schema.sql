-- SQLite migration 9 -> 10: reports and independent-control projections.
BEGIN IMMEDIATE;
CREATE TABLE report_sources (
 tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
 body TEXT NOT NULL, PRIMARY KEY(tenant,project,id));
CREATE TABLE report_policies (
 tenant TEXT NOT NULL, project TEXT NOT NULL, program TEXT NOT NULL, version INTEGER NOT NULL,
 body TEXT NOT NULL, PRIMARY KEY(tenant,project,program));
CREATE TABLE report_control_retries (
 tenant TEXT NOT NULL, actor TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
 result TEXT NOT NULL, PRIMARY KEY(tenant,actor,key));
CREATE TABLE report_uploads (
 tenant TEXT NOT NULL, project TEXT NOT NULL, actor TEXT NOT NULL, incarnation TEXT NOT NULL,
 upload TEXT NOT NULL, name TEXT NOT NULL, part INTEGER NOT NULL, parts INTEGER NOT NULL,
 digest TEXT NOT NULL, bytes INTEGER NOT NULL, blob TEXT NOT NULL, chunk_bytes INTEGER NOT NULL,
 PRIMARY KEY(tenant,project,actor,incarnation,upload,name,part));
CREATE TABLE report_revisions (
 tenant TEXT NOT NULL, project TEXT NOT NULL, report TEXT NOT NULL, revision INTEGER NOT NULL,
 incarnation TEXT NOT NULL, state TEXT NOT NULL, author TEXT NOT NULL, created INTEGER NOT NULL,
 body TEXT, qa TEXT, token TEXT, lease_until INTEGER,
 PRIMARY KEY(tenant,project,report,revision));
CREATE TABLE report_files (
 tenant TEXT NOT NULL, project TEXT NOT NULL, report TEXT NOT NULL, revision INTEGER NOT NULL,
 name TEXT NOT NULL, blob TEXT NOT NULL, bytes INTEGER NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(tenant,project,report,revision,name),
 FOREIGN KEY(tenant,project,report,revision) REFERENCES report_revisions(tenant,project,report,revision));
CREATE TABLE report_retries (
 tenant TEXT NOT NULL, actor TEXT NOT NULL, incarnation TEXT NOT NULL, key TEXT NOT NULL,
 request_hash TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(tenant,actor,incarnation,key));
CREATE TABLE report_deliveries (
 tenant TEXT NOT NULL, id TEXT NOT NULL, project TEXT NOT NULL, report TEXT NOT NULL,
 revision INTEGER NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE INDEX report_queue ON report_revisions(state,created);
PRAGMA user_version=10;
COMMIT;
