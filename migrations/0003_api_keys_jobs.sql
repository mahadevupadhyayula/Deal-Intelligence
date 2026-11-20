-- 0003_api_keys_jobs.sql
-- WHAT: API-key authentication table and the tenant-scoped jobs table.
-- WHERE: Read by api/auth.py (keys) and written by the API + ARQ worker (jobs).
-- WHY:
--   * api_keys is NOT under RLS: the key must be looked up BEFORE the tenant is known.
--     Only a SHA-256 hash is stored (keys are high-entropy random tokens, so a fast
--     unsalted hash is adequate and lets us index the lookup). app_user is read-only.
--   * jobs follows the Spec 002 pattern: composite PK with tenant_id + forced RLS.
--     The ARQ message carries only (tenant_id, job_id); the payload lives here, so Redis
--     never holds tenant data and the DB remains the source of truth.

CREATE TABLE api_keys (
    key_hash   text PRIMARY KEY,
    tenant_id  text NOT NULL REFERENCES tenants,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);
GRANT SELECT ON api_keys TO app_user;

CREATE TABLE jobs (
    tenant_id  text NOT NULL REFERENCES tenants,
    job_id     uuid NOT NULL,
    kind       text NOT NULL,
    status     text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    payload    jsonb NOT NULL,
    result     jsonb,
    error      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, job_id)
);
CREATE INDEX jobs_status_idx ON jobs (tenant_id, status);

GRANT SELECT, INSERT, UPDATE ON jobs TO app_user;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON jobs
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
