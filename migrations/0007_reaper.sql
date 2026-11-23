-- 0007_reaper.sql
-- WHAT: A narrow, privileged function that fails jobs stranded in `running` after a hard worker
--   kill (OOM, node loss, SIGKILL), plus the NOLOGIN role that owns it.
-- WHERE: Called by the API's background reaper loop (Spec 010) as app_user; also callable by tests.
-- WHY:
--   * A stranded `running` row counts toward the tenant's active-job cap forever (Spec 006), and
--     on Kubernetes hard kills are routine. Finding such rows is inherently CROSS-TENANT, but
--     every other statement in the system must keep forced RLS.
--   * So the one privileged operation is a single reviewable SQL object: a SECURITY DEFINER
--     function owned by `job_reaper` (NOLOGIN, BYPASSRLS, rights on jobs only). app_user keeps
--     NOBYPASSRLS and can only EXECUTE this function; it cannot read other tenants' rows
--     through it (it returns ids only, never payloads or results).
--   * `search_path` is pinned (definer functions must not trust the caller's path).
--   * max_age below 60 seconds is rejected: a typo like '0 seconds' would fail every live job.
--   * Concurrent callers are safe without advisory locks: the UPDATE locks each row, and the
--     loser re-checks `status = 'running'` after the winner commits, so each job is reaped once.
--   * NOTE for Spec 011: creating a BYPASSRLS role needs superuser; managed Postgres (RDS) may
--     restrict this and the cloud overlay must run this migration with an account that can.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'job_reaper') THEN
        CREATE ROLE job_reaper NOLOGIN NOSUPERUSER BYPASSRLS;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO job_reaper;
GRANT SELECT, UPDATE ON jobs TO job_reaper;

CREATE OR REPLACE FUNCTION reap_stale_jobs(max_age interval)
RETURNS TABLE (tenant_id text, job_id uuid)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $fn$
BEGIN
    IF max_age < interval '60 seconds' THEN
        RAISE EXCEPTION 'max_age must be at least 60 seconds';
    END IF;
    RETURN QUERY
        UPDATE jobs j
           SET status = 'failed', error = 'worker lost; resubmit', updated_at = now()
         WHERE j.status = 'running' AND j.updated_at < now() - max_age
        RETURNING j.tenant_id, j.job_id;
END
$fn$;

ALTER FUNCTION reap_stale_jobs(interval) OWNER TO job_reaper;
REVOKE ALL ON FUNCTION reap_stale_jobs(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reap_stale_jobs(interval) TO app_user;
