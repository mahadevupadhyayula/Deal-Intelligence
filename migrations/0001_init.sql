-- 0001_init.sql
-- WHAT: Core tenant-scoped tables (tenants, leads, deals, conflicts, handoff_briefs).
-- WHERE: First migration; repositories in agents/repos/ read/write these tables.
-- WHY:
--   * Every table carries tenant_id and PRIMARY/FOREIGN keys are COMPOSITE with it,
--     so the database itself rejects cross-tenant references.
--   * Pydantic JSON lives in a `data jsonb` column (schemas will evolve); only the
--     columns we filter on are promoted to real columns.
--   * Indexes lead with tenant_id so every tenant-filtered query can use them.

CREATE EXTENSION IF NOT EXISTS vector;   -- needed from Spec 006; verified here (admin only)

CREATE TABLE tenants (
    tenant_id  text PRIMARY KEY CHECK (tenant_id <> ''),
    name       text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE leads (
    tenant_id  text NOT NULL REFERENCES tenants,
    lead_id    text NOT NULL,
    data       jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, lead_id)
);

CREATE TABLE deals (
    tenant_id  text NOT NULL REFERENCES tenants,
    deal_id    text NOT NULL,
    stage      text NOT NULL,
    data       jsonb NOT NULL,          -- DealState minus conflicts (stored in `conflicts`)
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, deal_id)
);
CREATE INDEX deals_stage_idx ON deals (tenant_id, stage);

CREATE TABLE conflicts (
    tenant_id   text NOT NULL,
    conflict_id text NOT NULL,
    deal_id     text NOT NULL,
    risk        text NOT NULL,
    status      text NOT NULL,
    data        jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, conflict_id),
    -- Composite FK: a conflict can only point at a deal in the SAME tenant.
    FOREIGN KEY (tenant_id, deal_id) REFERENCES deals ON DELETE CASCADE
);
CREATE INDEX conflicts_deal_idx ON conflicts (tenant_id, deal_id);
CREATE INDEX conflicts_status_idx ON conflicts (tenant_id, status);

CREATE TABLE handoff_briefs (
    tenant_id    text NOT NULL,
    deal_id      text NOT NULL,
    data         jsonb NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, deal_id),
    FOREIGN KEY (tenant_id, deal_id) REFERENCES deals ON DELETE CASCADE
);
