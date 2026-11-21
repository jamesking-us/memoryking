BEGIN IMMEDIATE;
-- The job frontier looks jobs up by position; without this index each lookup scans the tenant's jobs.
CREATE INDEX IF NOT EXISTS jobs_position ON jobs(tenant,position);
PRAGMA user_version=14;
COMMIT;
