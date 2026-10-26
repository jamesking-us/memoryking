BEGIN IMMEDIATE;
CREATE TABLE viewer_bindings (
 tenant TEXT NOT NULL REFERENCES tenants(tenant), principal TEXT NOT NULL,
 issuer TEXT NOT NULL, user_id TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 generation INTEGER NOT NULL CHECK(generation>=1), version INTEGER NOT NULL CHECK(version>=1),
 PRIMARY KEY(tenant,principal), UNIQUE(tenant,issuer,user_id));
CREATE TABLE viewer_grants (
 tenant TEXT NOT NULL, principal TEXT NOT NULL, project TEXT NOT NULL,
 starts INTEGER, expires INTEGER,
 PRIMARY KEY(tenant,principal,project),
 FOREIGN KEY(tenant,principal) REFERENCES viewer_bindings(tenant,principal),
 CHECK(starts IS NULL OR expires IS NULL OR starts<expires));
CREATE TABLE viewer_admin_retries (
 tenant TEXT NOT NULL, actor TEXT NOT NULL, key TEXT NOT NULL,
 request_hash TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(tenant,actor,key));
PRAGMA user_version=9;
COMMIT;
