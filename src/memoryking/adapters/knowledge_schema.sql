BEGIN IMMEDIATE;
CREATE TABLE knowledge_state (tenant TEXT PRIMARY KEY REFERENCES tenants,
 generation INTEGER NOT NULL DEFAULT 0, recovery_gap INTEGER NOT NULL DEFAULT 0);
INSERT INTO knowledge_state(tenant) SELECT tenant FROM tenants;
CREATE TABLE families (tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL,
 project TEXT NOT NULL, logical_key TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,project,logical_key));
CREATE TABLE revisions (tenant TEXT NOT NULL, id TEXT NOT NULL, family TEXT NOT NULL,
 body TEXT, body_hash TEXT NOT NULL, kind TEXT NOT NULL, issuer TEXT NOT NULL,
 created INTEGER NOT NULL, origin TEXT NOT NULL, purged INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,family) REFERENCES families(tenant,id),
 FOREIGN KEY(tenant,created) REFERENCES events(tenant,position));
CREATE TABLE evidence_spans (tenant TEXT NOT NULL, revision TEXT NOT NULL,
 observation TEXT NOT NULL, start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL,
 content_hash TEXT NOT NULL, PRIMARY KEY(tenant,revision,observation,start_byte,end_byte),
 FOREIGN KEY(tenant,revision) REFERENCES revisions(tenant,id),
 FOREIGN KEY(tenant,observation) REFERENCES observations(tenant,id));
CREATE TABLE lifecycle (tenant TEXT NOT NULL, revision TEXT NOT NULL, position INTEGER NOT NULL,
 action TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(tenant,position),
 FOREIGN KEY(tenant,revision) REFERENCES revisions(tenant,id),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position));
CREATE TABLE replacements (tenant TEXT NOT NULL, predecessor TEXT NOT NULL, successor TEXT NOT NULL,
 position INTEGER NOT NULL, selector TEXT NOT NULL, valid_start INTEGER, valid_end INTEGER,
 PRIMARY KEY(tenant,predecessor,successor,position),
 FOREIGN KEY(tenant,predecessor) REFERENCES revisions(tenant,id),
 FOREIGN KEY(tenant,successor) REFERENCES revisions(tenant,id),
 FOREIGN KEY(tenant,position) REFERENCES events(tenant,position));
CREATE TABLE conflicts (tenant TEXT NOT NULL, left_revision TEXT NOT NULL,
 right_revision TEXT NOT NULL, position INTEGER NOT NULL,
 PRIMARY KEY(tenant,left_revision,right_revision),
 FOREIGN KEY(tenant,left_revision) REFERENCES revisions(tenant,id),
 FOREIGN KEY(tenant,right_revision) REFERENCES revisions(tenant,id));
CREATE TABLE attestations (tenant TEXT NOT NULL, id TEXT NOT NULL, revision TEXT NOT NULL,
 actor TEXT NOT NULL, position INTEGER NOT NULL, expires INTEGER NOT NULL, statement TEXT,
 PRIMARY KEY(tenant,id), FOREIGN KEY(tenant,revision) REFERENCES revisions(tenant,id));
PRAGMA user_version=2;
COMMIT;
