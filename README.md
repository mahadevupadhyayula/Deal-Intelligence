# Deal Intelligence — Multi-Agent B2B SaaS

Multi-tenant platform where four cooperating agents research leads, clean CRM data with human approval, build a deal wiki, and hand won deals to implementation. Built spec-by-spec with 3-Gate Verification (see [CLAUDE.md](CLAUDE.md)).

## System Architecture

```
                 ┌──────────────┐        ┌──────────────────┐
  CRM Webhooks ─▶│   FastAPI    │──enq──▶│  Redis / ARQ     │
  Streamlit UI ─▶│ (never blocks)│        │  (or SQS)        │
                 └──────┬───────┘        └────────┬─────────┘
                        │ read status             │ KEDA scales workers
                        │                         ▼
                        │          ┌──────────────────────────────┐
                        │          │ Workers (LangGraph agents)   │
                        │          │  ┌────────────────────────┐  │
                        │          │  │ Lead Research          │  │
                        │          │  │ CRM Hygiene (HITL) ◀───┼──┼── human approve/reject
                        │          │  │ LLM Wiki               │  │
                        │          │  │ Sales→Impl. Handoff    │  │
                        │          │  └────────────────────────┘  │
                        │          └───────┬──────────────┬───────┘
                        ▼                  ▼              ▼
                 ┌──────────────────────────────────────────────┐      ┌───────────┐
                 │ PostgreSQL: tenant-scoped tables + pgvector  │      │ LangSmith │
                 │ + PostgresSaver checkpoints (thread_id=tenant)│      │  traces   │
                 └──────────────────────────────────────────────┘      └───────────┘
                 ┌──────────────────────────────────────────────┐
                 │ OAuth token vault (KMS/Secrets Manager)      │
                 └──────────────────────────────────────────────┘
```

## Roadmap — 5 Phases / 16 Specs

| Phase | Spec | Title | Status |
|-------|------|-------|--------|
| 1. Foundation | 001 | Foundation: `pyproject`, config, Pydantic schemas, DB + pgvector | Done |
| 1. Foundation | 002 | Tenant-scoped data layer & migrations | Done |
| 1. Foundation | 003 | FastAPI skeleton + ARQ enqueue/worker plumbing | Done |
| 2. Core Agents | 004 | Lead Research agent (LangGraph) | Drafted |
| 2. Core Agents | 005 | LLM Wiki agent (pgvector ingest + retrieval) | Planned |
| 2. Core Agents | 006 | CRM Hygiene agent: conflict detection | Planned |
| 2. Core Agents | 007 | CRM Hygiene HITL: `interrupt` + `PostgresSaver` resume | Planned |
| 2. Core Agents | 008 | Sales-to-Implementation Handoff agent | Planned |
| 3. Integrations | 009 | CRM connectors + OAuth token vault | Planned |
| 3. Integrations | 010 | Webhook ingestion: idempotency & rate limits | Planned |
| 4. Safety & Quality | 011 | Prompt-injection guardrails | Planned |
| 4. Safety & Quality | 012 | Golden datasets, trajectory evals, LangSmith | Planned |
| 4. Safety & Quality | 013 | Streamlit UI: review queue & deal views | Planned |
| 5. Production | 014 | Docker images + `docker compose` dev stack | Planned |
| 5. Production | 015 | Terraform: VPC, EKS, RDS, ElastiCache | Planned |
| 5. Production | 016 | K8s manifests, KEDA autoscaling, observability | Planned |

## Build Progress

| Date | Day | Spec | Gate 1 | Gate 2 | Gate 3 | Log |
|------|-----|------|--------|--------|--------|-----|
| 2026-11-20 | 01 | 001 | PASS (16) | N/A | PASS | [log](docs/build-logs/2026-11-20-day-01.md) |
| 2026-11-20 | 02 | 002 | PASS (27) | N/A | PASS | [log](docs/build-logs/2026-11-20-day-02.md) |
| 2026-11-20 | 03 | 003 | PASS (59) | N/A | PASS | [log](docs/build-logs/2026-11-20-day-03.md) |

## Workflow

1. Say **"Draft Spec XXX"** → spec appears in `specs/` for review.
2. Approve → implement → run 3-Gate Verification → write build log → update table above.
