BEGIN IMMEDIATE;
-- Indexes for the scans that tables which only grow would otherwise make linear.
CREATE INDEX IF NOT EXISTS rendered_retained ON rendered_artifacts(tenant,expires) WHERE state='retained';
CREATE INDEX IF NOT EXISTS rendered_retained_owner ON rendered_artifacts(tenant,principal,created,id) WHERE state='retained';
CREATE INDEX IF NOT EXISTS rendered_retained_age ON rendered_artifacts(tenant,created,id) WHERE state='retained';
CREATE INDEX IF NOT EXISTS rendered_tombstones ON rendered_artifacts(tenant,expires) WHERE state<>'retained';
CREATE INDEX IF NOT EXISTS evidence_spans_observation ON evidence_spans(tenant,observation);
CREATE INDEX IF NOT EXISTS events_time ON events(tenant,at_ms,position);
CREATE INDEX IF NOT EXISTS observations_source ON observations(tenant,project,source,position);
-- A purge looks for other observations naming the same blob before deleting its file.
CREATE INDEX IF NOT EXISTS observations_blob ON observations(tenant,blob);
CREATE INDEX IF NOT EXISTS retries_expiry ON retries(tenant,expires) WHERE result IS NOT NULL;
CREATE INDEX IF NOT EXISTS operations_open ON operation_intents(tenant) WHERE state IN ('intent_recorded','dispatching','unknown');
-- Which observations each access receipt lists, so a purge deletes exactly those receipts
-- without scanning a JSON column. Written with the receipt and removed with it.
CREATE TABLE IF NOT EXISTS receipt_observations (
 tenant TEXT NOT NULL, receipt TEXT NOT NULL, observation TEXT NOT NULL,
 PRIMARY KEY(tenant,receipt,observation),
 FOREIGN KEY(tenant,receipt) REFERENCES receipts(tenant,id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS receipt_observations_observation ON receipt_observations(tenant,observation);
INSERT INTO receipt_observations(tenant,receipt,observation)
 SELECT DISTINCT r.tenant,r.id,j.value FROM receipts r, json_each(r.observation_ids) j
 WHERE r.observation_ids<>'[]' ON CONFLICT DO NOTHING;
PRAGMA user_version=18;
COMMIT;
