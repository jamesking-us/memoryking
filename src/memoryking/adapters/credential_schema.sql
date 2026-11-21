BEGIN IMMEDIATE;
-- The current credential generation of a principal. A principal without a row is at generation 0,
-- which is what every credential issued before generations carries implicitly.
CREATE TABLE IF NOT EXISTS credential_generations (
 tenant TEXT NOT NULL REFERENCES tenants, principal TEXT NOT NULL, generation INTEGER NOT NULL,
 PRIMARY KEY(tenant,principal));
PRAGMA user_version=15;
COMMIT;
