BEGIN IMMEDIATE;
-- Consolidation: one row per background pass in which a model drafted claims from recent
-- observations. The newest admitted `to_position` is the project's consolidation frontier.
-- Rows hold counts and identifiers only, never text.
CREATE TABLE IF NOT EXISTS consolidation_runs (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 principal TEXT NOT NULL, state TEXT NOT NULL, from_position INTEGER NOT NULL,
 to_position INTEGER NOT NULL, observations INTEGER NOT NULL, prompt_version TEXT NOT NULL,
 permit TEXT, provider TEXT, model TEXT, filed INTEGER NOT NULL, dropped TEXT NOT NULL,
 created INTEGER NOT NULL, closed INTEGER, PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS consolidation_runs_frontier ON consolidation_runs(tenant,project,state,to_position);
PRAGMA user_version=18;
COMMIT;
