-- 0006_tenant_usage.sql
-- WHAT: Per-tenant, per-UTC-day counters of provider calls (LLM calls, searches).
-- WHERE: Incremented by the worker after each provider-using job (workers/tasks.py, Spec 006);
--   read by API admission and the worker's run-start check to enforce daily quotas.
-- WHY:
--   * One row per (tenant, day) with an atomic upsert-increment keeps counting race-free when
--     several workers finish jobs for the same tenant at once.
--   * Calls (not dollars) are metered: they are exact, provider-independent and known to us
--     without parsing provider responses; LangSmith reports cost.
--   * Same forced-RLS pattern as every tenant table (tenant_id = owner account), so a tenant
--     can never read or bump another tenant's usage.

CREATE TABLE tenant_usage (
    tenant_id  text NOT NULL REFERENCES tenants,
    day        date NOT NULL,
    llm_calls  integer NOT NULL DEFAULT 0 CHECK (llm_calls >= 0),
    searches   integer NOT NULL DEFAULT 0 CHECK (searches >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, day)
);

GRANT SELECT, INSERT, UPDATE ON tenant_usage TO app_user;
ALTER TABLE tenant_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_usage FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant_usage
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
