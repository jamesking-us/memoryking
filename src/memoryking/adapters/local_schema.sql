CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tenants (tenant TEXT PRIMARY KEY, position INTEGER NOT NULL DEFAULT 0,
 policy_epoch INTEGER NOT NULL DEFAULT 0, purge_epoch INTEGER NOT NULL DEFAULT 0);
CREATE TABLE grants (tenant TEXT NOT NULL REFERENCES tenants, principal TEXT NOT NULL,
 project TEXT NOT NULL, capabilities TEXT NOT NULL, starts INTEGER, expires INTEGER,
 PRIMARY KEY(tenant,principal,project));
CREATE TABLE events (tenant TEXT NOT NULL REFERENCES tenants, position INTEGER NOT NULL,
 event_id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, origin TEXT NOT NULL,
 at_ms INTEGER NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(tenant,position), UNIQUE(tenant,event_id));
CREATE TABLE observations (tenant TEXT NOT NULL, id TEXT NOT NULL, project TEXT NOT NULL,
 principal TEXT NOT NULL, source TEXT NOT NULL, source_event TEXT NOT NULL,
 payload_hash TEXT NOT NULL, blob TEXT, readers TEXT, position INTEGER NOT NULL,
 classification TEXT NOT NULL, observed_at_ms INTEGER NOT NULL,
 origin TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,principal,source,source_event),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position));
CREATE VIRTUAL TABLE lexical USING fts5(tenant UNINDEXED, id UNINDEXED, text);
CREATE TABLE retries (tenant TEXT NOT NULL REFERENCES tenants, incarnation TEXT NOT NULL,
 namespace TEXT NOT NULL, principal TEXT NOT NULL, operation TEXT NOT NULL,
 key TEXT NOT NULL, hash TEXT NOT NULL, result TEXT, observation TEXT,
 expires INTEGER NOT NULL, PRIMARY KEY(tenant,incarnation,namespace,principal,operation,key));
CREATE TABLE jobs (tenant TEXT NOT NULL, id TEXT NOT NULL, position INTEGER NOT NULL,
 observation TEXT, incarnation TEXT NOT NULL, generation INTEGER NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, fence INTEGER NOT NULL DEFAULT 0,
 lease_until INTEGER NOT NULL DEFAULT 0, available INTEGER NOT NULL DEFAULT 0,
 purge_epoch INTEGER NOT NULL, revision INTEGER NOT NULL, principal TEXT NOT NULL,
 PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,position) REFERENCES events(tenant,position),
 FOREIGN KEY(tenant,observation) REFERENCES observations(tenant,id));
CREATE TABLE projections (tenant TEXT PRIMARY KEY REFERENCES tenants, incarnation TEXT NOT NULL,
 channel TEXT NOT NULL, configuration TEXT NOT NULL, generation INTEGER NOT NULL,
 frontier INTEGER NOT NULL DEFAULT 0);
CREATE TABLE projection_items (tenant TEXT NOT NULL, observation TEXT NOT NULL,
 incarnation TEXT NOT NULL, generation INTEGER NOT NULL, source_revision INTEGER NOT NULL,
 blob_hash TEXT NOT NULL, byte_count INTEGER NOT NULL, PRIMARY KEY(tenant,observation),
 FOREIGN KEY(tenant,observation) REFERENCES observations(tenant,id));
CREATE TABLE receipts (tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL,
 principal TEXT NOT NULL, incarnation TEXT NOT NULL, evaluated INTEGER NOT NULL,
 deadline INTEGER NOT NULL, policy_epoch INTEGER NOT NULL, purge_epoch INTEGER NOT NULL,
 response_hash TEXT NOT NULL, observation_ids TEXT NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE TABLE retired_sources (tenant TEXT NOT NULL REFERENCES tenants, principal TEXT NOT NULL,
 source TEXT NOT NULL, source_event TEXT NOT NULL,
 PRIMARY KEY(tenant,principal,source,source_event));
PRAGMA user_version=1;
