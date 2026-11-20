# CLAUDE.md — Deal Intelligence Multi-Agent SaaS

Multi-tenant B2B SaaS of cooperating agents that research leads, clean CRM data (with human-in-the-loop), maintain a deal wiki, and produce sales-to-implementation handoffs. Built with Specification-Driven Development (SDD). These rules are non-negotiable.

## a) Tech Stack

- Python 3.11+, Pydantic v2 (+ `pydantic-settings`)
- LangGraph, LangChain, LangSmith (tracing/evals)
- FastAPI (API), Redis + ARQ (job queue)
- PostgreSQL with `pgvector` (embeddings) and `PostgresSaver` (LangGraph checkpoints for HITL)
- Streamlit (frontend)
- Docker, Terraform, AWS EKS + KEDA (queue-depth autoscaling)

## b) Architectural Boundaries

1. **Never block FastAPI.** Request handlers validate, then enqueue to Redis (ARQ) / SQS and return a job id. Agent runs happen in `workers/`.
2. **`tenant_id` everywhere.** Required on every DB query, every vector-search metadata filter, and embedded in every LangGraph `thread_id` (e.g. `"{tenant_id}:{deal_id}:{run_id}"`). No query path may omit it.
   - **Glossary:** `tenant_id` is the account that owns a row. Leads, ICPs, research and evaluations are data *owned by* a tenant; a lead is never a tenant. If a lead later becomes a customer, it gets its own new `tenant_id`, linked from the lead via a separate field (e.g. `converted_tenant_id`); never reuse `tenant_id` for that link.
3. **Stateless HITL.** Human approval pauses use LangGraph `interrupt` + `PostgresSaver`. No in-process memory of paused runs; any worker can resume any thread.
4. **Zero hardcoded secrets.** All config via environment / `pydantic-settings`; `.env` is git-ignored; `.env.example` documents keys.

## c) Educational Commenting Rule

Every code file begins with a module docstring covering:
- **WHAT** the file does,
- **WHERE** it sits in the system (upstream/downstream),
- **WHY** this pattern was chosen.

Add inline comments explaining trade-offs and data flow, not restating syntax.

## d) Rolling Spec Workflow

- Work **one spec at a time** from `/specs`.
- When the user says **"Draft Spec XXX"**, inspect `README.md`, `docs/ARCHITECTURE.md`, `specs/_TEMPLATE.md`, and existing code, then write `specs/XXX-<slug>.md` for review. **Do not code until the spec is approved.**

## e) 3-Gate Verification & Definition of Done

Before marking any spec complete, run a Verification Audit:
- **Gate 1 — Unit:** `pytest -v` (unit tests, Pydantic validation, `tenant_id` isolation tests).
- **Gate 2 — Agent behavior** (agent specs): verify LangGraph trajectory and interrupts against `tests/fixtures/` golden datasets and LangSmith traces.
- **Gate 3 — Live smoke:** run the spec's smoke test (`psql`, `curl`, `docker compose`, or `terraform plan`).
- Output a **Pass/Fail Verification Report** *before* writing the build log. Non-agent specs mark Gate 2 as N/A with a reason.

## f) Daily Build Logs

When wrapping up a session, create/update `docs/build-logs/YYYY-MM-DD-day-XX.md` with:
1. What was built
2. What it achieves
3. How it fits the larger framework (upstream/downstream flow)
4. 3-Gate Verification results

Then update the progress table in `README.md`.
