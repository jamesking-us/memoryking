BEGIN IMMEDIATE;
-- Lexical statistics: how many documents of a project hold each index term, and how long they are.
-- Whole-project figures (held observations count; a reader's visibility never changes them).
-- lexical_state says whether the full-text index and these figures are in the current format;
-- a tenant without a row that already holds observations was indexed before this format.
CREATE TABLE IF NOT EXISTS lexical_state (
 tenant TEXT NOT NULL REFERENCES tenants, state TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
 configuration TEXT NOT NULL, PRIMARY KEY(tenant));
CREATE TABLE IF NOT EXISTS lexical_projects (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, documents INTEGER NOT NULL,
 tokens INTEGER NOT NULL, PRIMARY KEY(tenant,project));
CREATE TABLE IF NOT EXISTS lexical_terms (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, term TEXT NOT NULL,
 documents INTEGER NOT NULL, PRIMARY KEY(tenant,project,term));
CREATE INDEX IF NOT EXISTS observations_position ON observations(tenant,position);
PRAGMA user_version=16;
COMMIT;
