-- Native PostgreSQL shared schema revision 1. No SQLite file or locking emulation.
-- Migration role owns this schema. Runtime roles receive DML, never ownership/TRUNCATE.
CREATE TABLE shared_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tenants (tenant TEXT PRIMARY KEY, position BIGINT NOT NULL DEFAULT 0,
 policy_epoch BIGINT NOT NULL DEFAULT 0, purge_epoch BIGINT NOT NULL DEFAULT 0);
CREATE TABLE grants (tenant TEXT NOT NULL REFERENCES tenants, principal TEXT NOT NULL,
 project TEXT NOT NULL, capabilities TEXT NOT NULL, starts BIGINT, expires BIGINT,
 PRIMARY KEY(tenant,principal,project));
CREATE TABLE events (tenant TEXT NOT NULL REFERENCES tenants, position BIGINT NOT NULL,
 event_id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, origin TEXT NOT NULL,
 at_ms BIGINT NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(tenant,position), UNIQUE(tenant,event_id));
CREATE TABLE observations (tenant TEXT NOT NULL, id TEXT NOT NULL, project TEXT NOT NULL,
 principal TEXT NOT NULL, source TEXT NOT NULL, source_event TEXT NOT NULL,
 payload_hash TEXT NOT NULL, blob TEXT, readers TEXT, position BIGINT NOT NULL,
 classification TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
 origin TEXT NOT NULL, revision BIGINT NOT NULL DEFAULT 1, deleted BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,principal,source,source_event),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position));
CREATE TABLE lexical (tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,id) REFERENCES observations(tenant,id));
CREATE INDEX lexical_search ON lexical USING GIN (to_tsvector('simple',text));
CREATE TABLE retries (tenant TEXT NOT NULL REFERENCES tenants, incarnation TEXT NOT NULL,
 namespace TEXT NOT NULL, principal TEXT NOT NULL, operation TEXT NOT NULL,
 key TEXT NOT NULL, hash TEXT NOT NULL, result TEXT, observation TEXT,
 expires BIGINT NOT NULL, PRIMARY KEY(tenant,incarnation,namespace,principal,operation,key));
CREATE TABLE jobs (tenant TEXT NOT NULL, id TEXT NOT NULL, position BIGINT NOT NULL,
 observation TEXT, incarnation TEXT NOT NULL, generation BIGINT NOT NULL,
 state TEXT NOT NULL, attempts BIGINT NOT NULL DEFAULT 0, fence BIGINT NOT NULL DEFAULT 0,
 lease_until BIGINT NOT NULL DEFAULT 0, available BIGINT NOT NULL DEFAULT 0,
 purge_epoch BIGINT NOT NULL, revision BIGINT NOT NULL, principal TEXT NOT NULL,
 PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,position) REFERENCES events(tenant,position),
 FOREIGN KEY(tenant,observation) REFERENCES observations(tenant,id));
CREATE TABLE projections (tenant TEXT PRIMARY KEY REFERENCES tenants, incarnation TEXT NOT NULL,
 channel TEXT NOT NULL, configuration TEXT NOT NULL, generation BIGINT NOT NULL,
 frontier BIGINT NOT NULL DEFAULT 0);
CREATE TABLE projection_items (tenant TEXT NOT NULL, observation TEXT NOT NULL,
 incarnation TEXT NOT NULL, generation BIGINT NOT NULL, source_revision BIGINT NOT NULL,
 blob_hash TEXT NOT NULL, byte_count BIGINT NOT NULL, PRIMARY KEY(tenant,observation),
 FOREIGN KEY(tenant,observation) REFERENCES observations(tenant,id));
CREATE TABLE receipts (tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL,
 principal TEXT NOT NULL, incarnation TEXT NOT NULL, evaluated BIGINT NOT NULL,
 deadline BIGINT NOT NULL, policy_epoch BIGINT NOT NULL, purge_epoch BIGINT NOT NULL,
 response_hash TEXT NOT NULL, observation_ids TEXT NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE TABLE retired_sources (tenant TEXT NOT NULL REFERENCES tenants, principal TEXT NOT NULL,
 source TEXT NOT NULL, source_event TEXT NOT NULL,
 PRIMARY KEY(tenant,principal,source,source_event));

CREATE TABLE knowledge_state (tenant TEXT PRIMARY KEY REFERENCES tenants,
 generation BIGINT NOT NULL DEFAULT 0, recovery_gap BIGINT NOT NULL DEFAULT 0);

CREATE TABLE families (tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL,
 project TEXT NOT NULL, logical_key TEXT NOT NULL, version BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,project,logical_key));
CREATE TABLE revisions (tenant TEXT NOT NULL, id TEXT NOT NULL, family TEXT NOT NULL,
 body TEXT, body_hash TEXT NOT NULL, kind TEXT NOT NULL, issuer TEXT NOT NULL,
 created BIGINT NOT NULL, origin TEXT NOT NULL, purged BIGINT NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,family) REFERENCES families(tenant,id),
 FOREIGN KEY(tenant,created) REFERENCES events(tenant,position));
CREATE TABLE evidence_spans (tenant TEXT NOT NULL, revision TEXT NOT NULL,
 observation TEXT NOT NULL, start_byte BIGINT NOT NULL, end_byte BIGINT NOT NULL,
 content_hash TEXT NOT NULL, PRIMARY KEY(tenant,revision,observation,start_byte,end_byte),
 FOREIGN KEY(tenant,revision) REFERENCES revisions(tenant,id),
 FOREIGN KEY(tenant,observation) REFERENCES observations(tenant,id));
CREATE TABLE lifecycle (tenant TEXT NOT NULL, revision TEXT NOT NULL, position BIGINT NOT NULL,
 action TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(tenant,position),
 FOREIGN KEY(tenant,revision) REFERENCES revisions(tenant,id),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position));
CREATE TABLE replacements (tenant TEXT NOT NULL, predecessor TEXT NOT NULL, successor TEXT NOT NULL,
 position BIGINT NOT NULL, selector TEXT NOT NULL, valid_start BIGINT, valid_end BIGINT,
 PRIMARY KEY(tenant,predecessor,successor,position),
 FOREIGN KEY(tenant,predecessor) REFERENCES revisions(tenant,id),
 FOREIGN KEY(tenant,successor) REFERENCES revisions(tenant,id),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position));
CREATE TABLE conflicts (tenant TEXT NOT NULL, left_revision TEXT NOT NULL,
 right_revision TEXT NOT NULL, position BIGINT NOT NULL,
 PRIMARY KEY(tenant,left_revision,right_revision),
 FOREIGN KEY(tenant,left_revision) REFERENCES revisions(tenant,id),
 FOREIGN KEY(tenant,right_revision) REFERENCES revisions(tenant,id));
CREATE TABLE attestations (tenant TEXT NOT NULL, id TEXT NOT NULL, revision TEXT NOT NULL,
 actor TEXT NOT NULL, position BIGINT NOT NULL, expires BIGINT NOT NULL, statement TEXT,
 PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,revision) REFERENCES revisions(tenant,id));

CREATE TABLE IF NOT EXISTS rendered_artifacts (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, principal TEXT NOT NULL,
 project TEXT NOT NULL, incarnation TEXT NOT NULL, created BIGINT NOT NULL,
 expires BIGINT NOT NULL, state TEXT NOT NULL, body BYTEA, body_hash TEXT NOT NULL,
 request_json TEXT, diagnostics TEXT, dependencies TEXT NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS rendered_owner ON rendered_artifacts(tenant,principal,project,created);

CREATE TABLE IF NOT EXISTS checkpoint_streams (
 tenant TEXT NOT NULL REFERENCES tenants, project TEXT NOT NULL, task TEXT NOT NULL,
 stream TEXT NOT NULL, writer TEXT NOT NULL, revision BIGINT NOT NULL,
 incarnation TEXT NOT NULL, latest TEXT,
 PRIMARY KEY(tenant,project,task,stream));
CREATE TABLE IF NOT EXISTS checkpoints (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 task TEXT NOT NULL, stream TEXT NOT NULL, revision BIGINT NOT NULL,
 writer TEXT NOT NULL, incarnation TEXT NOT NULL, created BIGINT NOT NULL,
 body TEXT, body_hash TEXT NOT NULL, readers TEXT NOT NULL, dependencies TEXT NOT NULL,
 state TEXT NOT NULL, position BIGINT NOT NULL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,project,task,stream,revision));
CREATE TABLE IF NOT EXISTS operation_intents (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, principal TEXT NOT NULL,
 project TEXT NOT NULL, task TEXT NOT NULL, stream TEXT NOT NULL,
 incarnation TEXT NOT NULL, target_hash TEXT NOT NULL, parameters_hash TEXT NOT NULL,
 method TEXT NOT NULL, provider_key TEXT, state TEXT NOT NULL, revision BIGINT NOT NULL,
 result_hash TEXT, evidence TEXT NOT NULL, created BIGINT NOT NULL, updated BIGINT NOT NULL,
 reconciled_incarnation TEXT,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS operations_scope ON operation_intents(tenant,project,task,stream);

CREATE TABLE IF NOT EXISTS handoffs (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, project TEXT NOT NULL,
 task TEXT NOT NULL, stream TEXT NOT NULL, checkpoint TEXT NOT NULL,
 sender TEXT NOT NULL, recipient TEXT NOT NULL, role TEXT NOT NULL, stage TEXT NOT NULL,
 bindings TEXT NOT NULL, incarnation TEXT NOT NULL, created BIGINT NOT NULL,
 expires BIGINT NOT NULL, state TEXT NOT NULL, stream_revision BIGINT NOT NULL,
 ack_view TEXT,
 PRIMARY KEY(tenant,id));
CREATE INDEX IF NOT EXISTS handoff_scope ON handoffs(tenant,project,task,stream,sender,recipient);
CREATE TABLE IF NOT EXISTS handoff_views (
 tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL, handoff TEXT NOT NULL,
 actor TEXT NOT NULL, mode TEXT NOT NULL, stamp TEXT NOT NULL, body_hash TEXT NOT NULL,
 status TEXT NOT NULL, created BIGINT NOT NULL,
 PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,handoff) REFERENCES handoffs(tenant,id));

CREATE TABLE IF NOT EXISTS repositories (tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL,
 project TEXT NOT NULL, remote TEXT NOT NULL, readers TEXT NOT NULL, generation BIGINT NOT NULL DEFAULT 0,
 current_incarnation TEXT, current_snapshot TEXT,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,project,remote));
CREATE TABLE IF NOT EXISTS repository_clones (tenant TEXT NOT NULL, repository TEXT NOT NULL, alias TEXT NOT NULL,
 path TEXT NOT NULL, device BIGINT NOT NULL, inode BIGINT NOT NULL,
 PRIMARY KEY(tenant,repository,alias), UNIQUE(tenant,path),
 FOREIGN KEY(tenant,repository) REFERENCES repositories(tenant,id));
CREATE TABLE IF NOT EXISTS repository_snapshots (tenant TEXT NOT NULL, incarnation TEXT NOT NULL, repository TEXT NOT NULL,
 id TEXT NOT NULL, generation BIGINT NOT NULL, position BIGINT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(tenant,incarnation,repository,id), FOREIGN KEY(tenant,repository) REFERENCES repositories(tenant,id));
CREATE TABLE IF NOT EXISTS code_policies (tenant TEXT NOT NULL, revision TEXT NOT NULL, repository TEXT NOT NULL,
 origin TEXT NOT NULL, version BIGINT NOT NULL, impact TEXT NOT NULL, dependencies TEXT NOT NULL,
 evidence TEXT NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(tenant,revision),
 FOREIGN KEY(tenant,revision) REFERENCES revisions(tenant,id), FOREIGN KEY(tenant,repository) REFERENCES repositories(tenant,id));
CREATE TABLE IF NOT EXISTS code_validations (tenant TEXT NOT NULL, incarnation TEXT NOT NULL, revision TEXT NOT NULL,
 snapshot TEXT NOT NULL, policy BIGINT NOT NULL, state TEXT NOT NULL, evidence TEXT NOT NULL,
 expires BIGINT NOT NULL, reason TEXT NOT NULL, generation BIGINT NOT NULL,
 PRIMARY KEY(tenant,incarnation,revision,snapshot,policy), FOREIGN KEY(tenant,revision) REFERENCES revisions(tenant,id));
CREATE TABLE IF NOT EXISTS repository_jobs (tenant TEXT NOT NULL, incarnation TEXT NOT NULL, repository TEXT NOT NULL,
 snapshot TEXT NOT NULL, generation BIGINT NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(tenant,incarnation,repository,snapshot,generation));

CREATE TABLE tenant_gates (tenant TEXT PRIMARY KEY REFERENCES tenants);
CREATE TABLE tenant_meta (tenant TEXT NOT NULL REFERENCES tenants, key TEXT NOT NULL,
 value TEXT NOT NULL, PRIMARY KEY(tenant,key));
CREATE VIEW meta WITH (security_invoker=true) AS
 SELECT key,value FROM tenant_meta WHERE tenant=current_setting('memoryking.tenant',true);

-- These two bounded data helpers preserve the existing relational service expressions.
CREATE FUNCTION json_type(source TEXT,path TEXT) RETURNS TEXT
 LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$
 SELECT jsonb_typeof(source::jsonb -> substring(path FROM 3));
$$;
CREATE FUNCTION json_each(source TEXT,path TEXT DEFAULT NULL) RETURNS TABLE(value TEXT)
 LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
 SELECT jsonb_array_elements_text(CASE WHEN jsonb_typeof(v)='array' THEN v ELSE '[]'::jsonb END)
 FROM (SELECT CASE WHEN path IS NULL THEN source::jsonb
                   ELSE source::jsonb -> substring(path FROM 3) END AS v) selected;
$$;

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenants
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE grants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON grants
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON events
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE observations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON observations
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE lexical ENABLE ROW LEVEL SECURITY;
ALTER TABLE lexical FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lexical
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE retries ENABLE ROW LEVEL SECURITY;
ALTER TABLE retries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON retries
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON jobs
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE projections ENABLE ROW LEVEL SECURITY;
ALTER TABLE projections FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON projections
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE projection_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE projection_items FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON projection_items
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON receipts
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE retired_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE retired_sources FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON retired_sources
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE knowledge_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_state FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON knowledge_state
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE families ENABLE ROW LEVEL SECURITY;
ALTER TABLE families FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON families
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON revisions
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE evidence_spans ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_spans FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON evidence_spans
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE lifecycle ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifecycle FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lifecycle
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE replacements ENABLE ROW LEVEL SECURITY;
ALTER TABLE replacements FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON replacements
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE conflicts ENABLE ROW LEVEL SECURITY;
ALTER TABLE conflicts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON conflicts
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE attestations ENABLE ROW LEVEL SECURITY;
ALTER TABLE attestations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attestations
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE rendered_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE rendered_artifacts FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rendered_artifacts
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE checkpoint_streams ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoint_streams FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON checkpoint_streams
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON checkpoints
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE operation_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE operation_intents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON operation_intents
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE handoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE handoffs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON handoffs
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE handoff_views ENABLE ROW LEVEL SECURITY;
ALTER TABLE handoff_views FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON handoff_views
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE repositories ENABLE ROW LEVEL SECURITY;
ALTER TABLE repositories FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON repositories
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE repository_clones ENABLE ROW LEVEL SECURITY;
ALTER TABLE repository_clones FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON repository_clones
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE repository_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE repository_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON repository_snapshots
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE code_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE code_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON code_policies
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE code_validations ENABLE ROW LEVEL SECURITY;
ALTER TABLE code_validations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON code_validations
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE repository_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE repository_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON repository_jobs
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE tenant_gates ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_gates FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_gates
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));

ALTER TABLE tenant_meta ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_meta FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_meta
 USING (tenant = current_setting('memoryking.tenant',true))
 WITH CHECK (tenant = current_setting('memoryking.tenant',true));
