-- 0002_rls.sql
-- WHAT: Creates the app_user role and enables FORCED row-level security.
-- WHERE: Applied by agents/migrate.py as the admin role; the API/workers connect as app_user.
-- WHY: Defense in depth. Even if application code forgets a WHERE tenant_id, Postgres
--   only exposes rows matching the transaction-local `app.tenant_id` setting. When the
--   setting is unset, current_setting(..., true) is NULL => no rows visible, writes rejected
--   (fail closed). NOTE: superusers and BYPASSRLS roles skip RLS, so app_user must be neither.
--   The role's password is set by the runner from env (never hardcoded here).

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO app_user;
-- Tenants are provisioned by admins, not by the app.
GRANT SELECT ON tenants TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON leads, deals, conflicts, handoff_briefs TO app_user;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['leads', 'deals', 'conflicts', 'handoff_briefs'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I
                 USING (tenant_id = current_setting(''app.tenant_id'', true))
                 WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))', t);
    END LOOP;
END
$$;
