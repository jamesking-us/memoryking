-- Shared migration 4 -> 5: reports; all tables remain tenant isolated.
CREATE TABLE report_sources (
 tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL, version BIGINT NOT NULL,
 body TEXT NOT NULL, PRIMARY KEY(tenant,project,id));
CREATE TABLE report_policies (
 tenant TEXT NOT NULL, project TEXT NOT NULL, program TEXT NOT NULL, version BIGINT NOT NULL,
 body TEXT NOT NULL, PRIMARY KEY(tenant,project,program));
CREATE TABLE report_control_retries (
 tenant TEXT NOT NULL, actor TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
 result TEXT NOT NULL, PRIMARY KEY(tenant,actor,key));
CREATE TABLE report_uploads (
 tenant TEXT NOT NULL, project TEXT NOT NULL, actor TEXT NOT NULL, incarnation TEXT NOT NULL,
 upload TEXT NOT NULL, name TEXT NOT NULL, part BIGINT NOT NULL, parts BIGINT NOT NULL,
 digest TEXT NOT NULL, bytes BIGINT NOT NULL, blob TEXT NOT NULL, chunk_bytes BIGINT NOT NULL,
 PRIMARY KEY(tenant,project,actor,incarnation,upload,name,part));
CREATE TABLE report_revisions (
 tenant TEXT NOT NULL, project TEXT NOT NULL, report TEXT NOT NULL, revision BIGINT NOT NULL,
 incarnation TEXT NOT NULL, state TEXT NOT NULL, author TEXT NOT NULL, created BIGINT NOT NULL,
 body TEXT, qa TEXT, token TEXT, lease_until BIGINT,
 PRIMARY KEY(tenant,project,report,revision));
CREATE TABLE report_files (
 tenant TEXT NOT NULL, project TEXT NOT NULL, report TEXT NOT NULL, revision BIGINT NOT NULL,
 name TEXT NOT NULL, blob TEXT NOT NULL, bytes BIGINT NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(tenant,project,report,revision,name),
 FOREIGN KEY(tenant,project,report,revision) REFERENCES report_revisions(tenant,project,report,revision));
CREATE TABLE report_retries (
 tenant TEXT NOT NULL, actor TEXT NOT NULL, incarnation TEXT NOT NULL, key TEXT NOT NULL,
 request_hash TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(tenant,actor,incarnation,key));
CREATE TABLE report_deliveries (
 tenant TEXT NOT NULL, id TEXT NOT NULL, project TEXT NOT NULL, report TEXT NOT NULL,
 revision BIGINT NOT NULL, state TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(tenant,id));
CREATE INDEX report_queue ON report_revisions(state,created);
ALTER TABLE report_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_sources FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON report_sources USING(tenant=current_setting('memoryking.tenant',true)) WITH CHECK(tenant=current_setting('memoryking.tenant',true));
ALTER TABLE report_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON report_policies USING(tenant=current_setting('memoryking.tenant',true)) WITH CHECK(tenant=current_setting('memoryking.tenant',true));
ALTER TABLE report_control_retries ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_control_retries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON report_control_retries USING(tenant=current_setting('memoryking.tenant',true)) WITH CHECK(tenant=current_setting('memoryking.tenant',true));
ALTER TABLE report_uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_uploads FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON report_uploads USING(tenant=current_setting('memoryking.tenant',true)) WITH CHECK(tenant=current_setting('memoryking.tenant',true));
ALTER TABLE report_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON report_revisions USING(tenant=current_setting('memoryking.tenant',true)) WITH CHECK(tenant=current_setting('memoryking.tenant',true));
ALTER TABLE report_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_files FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON report_files USING(tenant=current_setting('memoryking.tenant',true)) WITH CHECK(tenant=current_setting('memoryking.tenant',true));
ALTER TABLE report_retries ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_retries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON report_retries USING(tenant=current_setting('memoryking.tenant',true)) WITH CHECK(tenant=current_setting('memoryking.tenant',true));
ALTER TABLE report_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_deliveries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON report_deliveries USING(tenant=current_setting('memoryking.tenant',true)) WITH CHECK(tenant=current_setting('memoryking.tenant',true));
