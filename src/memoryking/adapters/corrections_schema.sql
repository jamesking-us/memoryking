BEGIN IMMEDIATE;
-- Corrections: a replacement capture names the earlier observation it supersedes (the old
-- one stays as evidence), a retirement request asks an admin to purge a record, and a pin
-- keeps an accepted claim or an observation in the session-start briefing.
CREATE TABLE IF NOT EXISTS supersessions (
 tenant TEXT NOT NULL REFERENCES tenants, replacement TEXT NOT NULL, superseded TEXT NOT NULL,
 project TEXT NOT NULL, principal TEXT NOT NULL, recorded INTEGER NOT NULL,
 PRIMARY KEY(tenant,replacement));
CREATE INDEX IF NOT EXISTS supersessions_old ON supersessions(tenant,superseded);
CREATE TABLE IF NOT EXISTS retirement_requests (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 observation TEXT NOT NULL, requester TEXT NOT NULL, note TEXT NOT NULL, state TEXT NOT NULL,
 requested INTEGER NOT NULL, decided_by TEXT, decided INTEGER, PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS retirement_requests_open ON retirement_requests(tenant,project,state,requested);
CREATE INDEX IF NOT EXISTS retirement_requests_record ON retirement_requests(tenant,observation);
CREATE TABLE IF NOT EXISTS pins (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, kind TEXT NOT NULL,
 target TEXT NOT NULL, pinned_by TEXT NOT NULL, pinned INTEGER NOT NULL,
 PRIMARY KEY(tenant,project,kind,target));
PRAGMA user_version=17;
COMMIT;
