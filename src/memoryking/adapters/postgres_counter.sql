-- Commit serial advancement runs as the table owner so worker and runtime roles need no
-- UPDATE right on tenant_meta. The function honors the session tenant like row security.
CREATE OR REPLACE FUNCTION memoryking.next_commit_serial(target text, maximum bigint)
RETURNS bigint
LANGUAGE sql
SECURITY DEFINER
SET search_path = memoryking, pg_catalog
AS $$
  UPDATE memoryking.tenant_meta SET value=(value::bigint+1)::text
  WHERE tenant=target AND key='commit_serial' AND value::bigint<maximum
    AND tenant=current_setting('memoryking.tenant', true)
  RETURNING value::bigint
$$;
REVOKE ALL ON FUNCTION memoryking.next_commit_serial(text, bigint) FROM PUBLIC;
