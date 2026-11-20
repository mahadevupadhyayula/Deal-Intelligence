-- 0004_icp_segments.sql
-- WHAT: Versioned, tenant-owned ICP segments (e.g. a primary and a secondary ICP).
-- WHERE: Written/read through agents/repos/icp.py (API routes + Lead Research agent).
-- WHY:
--   * Versions are immutable rows; changing a segment inserts a new version and flips
--     is_active. Evaluations can therefore cite the exact version they used.
--   * The partial unique index lets the DATABASE guarantee at most one active version per
--     (tenant, segment); application bugs cannot create two.
--   * tenant_id means "account that owns this row" (see CLAUDE.md glossary), same forced-RLS
--     pattern as every other tenant table.

CREATE TABLE icp_segments (
    tenant_id   text NOT NULL REFERENCES tenants,
    segment_key text NOT NULL CHECK (segment_key ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    version     int  NOT NULL CHECK (version >= 1),
    name        text NOT NULL,
    rank        text NOT NULL CHECK (rank IN ('primary', 'secondary')),
    definition  jsonb NOT NULL,          -- the validated IcpSegmentIn body
    is_active   boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, segment_key, version)
);
CREATE UNIQUE INDEX icp_segments_one_active
    ON icp_segments (tenant_id, segment_key) WHERE is_active;

GRANT SELECT, INSERT, UPDATE ON icp_segments TO app_user;
ALTER TABLE icp_segments ENABLE ROW LEVEL SECURITY;
ALTER TABLE icp_segments FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON icp_segments
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
