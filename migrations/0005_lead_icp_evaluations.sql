-- 0005_lead_icp_evaluations.sql
-- WHAT: History of ICP evaluations, one row per research run per lead.
-- WHERE: Written by the Lead Research agent's persist step (Spec 005); read by
--   api/routes/leads.py (latest per lead, history, ranked list).
-- WHY:
--   * History is kept (insert-only): evaluations cite the ICP versions they used and carry the
--     evidence quotes, so any past score can be audited even after the ICP changes.
--   * UNIQUE (tenant_id, job_id) makes persisting idempotent: a redelivered job cannot create
--     a second evaluation.
--   * Promoted columns (score, tier, best_rank, lead_id) serve the ranked list without
--     parsing JSON; the full evaluation lives in `data`.
--   * Same forced-RLS pattern as every tenant table (tenant_id = owner account).

CREATE TABLE lead_icp_evaluations (
    tenant_id        text NOT NULL REFERENCES tenants,
    evaluation_id    uuid NOT NULL,
    lead_id          text NOT NULL,
    job_id           uuid NOT NULL,
    best_segment_key text NOT NULL,
    best_rank        text NOT NULL CHECK (best_rank IN ('primary', 'secondary')),
    score            double precision NOT NULL,
    tier             text NOT NULL
        CHECK (tier IN ('strong', 'moderate', 'weak', 'not_fit', 'insufficient_data')),
    status           text NOT NULL CHECK (status IN ('complete', 'partial')),
    data             jsonb NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, evaluation_id),
    UNIQUE (tenant_id, job_id)
);
CREATE INDEX lead_eval_by_lead_idx ON lead_icp_evaluations (tenant_id, lead_id, created_at DESC);
CREATE INDEX lead_eval_by_tier_idx ON lead_icp_evaluations (tenant_id, tier, score DESC);

GRANT SELECT, INSERT ON lead_icp_evaluations TO app_user;
ALTER TABLE lead_icp_evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead_icp_evaluations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON lead_icp_evaluations
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
