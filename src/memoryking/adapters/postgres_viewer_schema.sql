-- Shared migration 3 -> 4: independent-journal viewer identity projections.
CREATE TABLE viewer_bindings (
 tenant TEXT NOT NULL REFERENCES tenants(tenant), principal TEXT NOT NULL,
 issuer TEXT NOT NULL, user_id TEXT NOT NULL, enabled BIGINT NOT NULL CHECK(enabled IN (0,1)),
 generation BIGINT NOT NULL CHECK(generation>=1), version BIGINT NOT NULL CHECK(version>=1),
 PRIMARY KEY(tenant,principal), UNIQUE(tenant,issuer,user_id));
CREATE TABLE viewer_grants (
 tenant TEXT NOT NULL, principal TEXT NOT NULL, project TEXT NOT NULL,
 starts BIGINT, expires BIGINT,
 PRIMARY KEY(tenant,principal,project),
 FOREIGN KEY(tenant,principal) REFERENCES viewer_bindings(tenant,principal),
 CHECK(starts IS NULL OR expires IS NULL OR starts<expires));
CREATE TABLE viewer_admin_retries (
 tenant TEXT NOT NULL, actor TEXT NOT NULL, key TEXT NOT NULL,
 request_hash TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(tenant,actor,key));
ALTER TABLE viewer_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE viewer_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON viewer_bindings
 USING(tenant=current_setting('memoryking.tenant',true))
 WITH CHECK(tenant=current_setting('memoryking.tenant',true));
ALTER TABLE viewer_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE viewer_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON viewer_grants
 USING(tenant=current_setting('memoryking.tenant',true))
 WITH CHECK(tenant=current_setting('memoryking.tenant',true));
ALTER TABLE viewer_admin_retries ENABLE ROW LEVEL SECURITY;
ALTER TABLE viewer_admin_retries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON viewer_admin_retries
 USING(tenant=current_setting('memoryking.tenant',true))
 WITH CHECK(tenant=current_setting('memoryking.tenant',true));
