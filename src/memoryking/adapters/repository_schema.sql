BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS repositories (tenant TEXT NOT NULL REFERENCES tenants, id TEXT NOT NULL,
 project TEXT NOT NULL, remote TEXT NOT NULL, readers TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
 current_incarnation TEXT, current_snapshot TEXT,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,project,remote));
CREATE TABLE IF NOT EXISTS repository_clones (tenant TEXT NOT NULL, repository TEXT NOT NULL, alias TEXT NOT NULL,
 path TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
 PRIMARY KEY(tenant,repository,alias), UNIQUE(tenant,path),
 FOREIGN KEY(tenant,repository) REFERENCES repositories(tenant,id));
CREATE TABLE IF NOT EXISTS repository_snapshots (tenant TEXT NOT NULL, incarnation TEXT NOT NULL, repository TEXT NOT NULL,
 id TEXT NOT NULL, generation INTEGER NOT NULL, position INTEGER NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(tenant,incarnation,repository,id), FOREIGN KEY(tenant,repository) REFERENCES repositories(tenant,id));
CREATE TABLE IF NOT EXISTS code_policies (tenant TEXT NOT NULL, revision TEXT NOT NULL, repository TEXT NOT NULL,
 origin TEXT NOT NULL, version INTEGER NOT NULL, impact TEXT NOT NULL, dependencies TEXT NOT NULL,
 evidence TEXT NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(tenant,revision),
 FOREIGN KEY(tenant,revision) REFERENCES revisions(tenant,id), FOREIGN KEY(tenant,repository) REFERENCES repositories(tenant,id));
CREATE TABLE IF NOT EXISTS code_validations (tenant TEXT NOT NULL, incarnation TEXT NOT NULL, revision TEXT NOT NULL,
 snapshot TEXT NOT NULL, policy INTEGER NOT NULL, state TEXT NOT NULL, evidence TEXT NOT NULL,
 expires INTEGER NOT NULL, reason TEXT NOT NULL, generation INTEGER NOT NULL,
 PRIMARY KEY(tenant,incarnation,revision,snapshot,policy), FOREIGN KEY(tenant,revision) REFERENCES revisions(tenant,id));
CREATE TABLE IF NOT EXISTS repository_jobs (tenant TEXT NOT NULL, incarnation TEXT NOT NULL, repository TEXT NOT NULL,
 snapshot TEXT NOT NULL, generation INTEGER NOT NULL, state TEXT NOT NULL,
 PRIMARY KEY(tenant,incarnation,repository,snapshot,generation));
PRAGMA user_version=6;
COMMIT;
