BEGIN IMMEDIATE;
-- Hot-path indexes for tables that only grow: queue scans, per-project candidate windows
-- and lookups by observation during purge, drain and projection maintenance.
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(tenant,state,available,position);
CREATE INDEX IF NOT EXISTS jobs_observation ON jobs(tenant,observation);
CREATE INDEX IF NOT EXISTS observations_scope ON observations(tenant,project,position);
CREATE INDEX IF NOT EXISTS retries_observation ON retries(tenant,observation);
CREATE INDEX IF NOT EXISTS embedding_items_observation ON embedding_items(tenant,observation);
CREATE INDEX IF NOT EXISTS receipts_evaluated ON receipts(tenant,evaluated);
PRAGMA user_version=9;
COMMIT;
