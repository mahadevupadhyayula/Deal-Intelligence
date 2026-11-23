# Deal Intelligence — Multi-Agent B2B SaaS

Multi-tenant platform where four cooperating agents research leads, clean CRM data with human approval, build a deal wiki, and hand won deals to implementation. Built spec-by-spec, one agent slice at a time, with 3-Gate Verification (see [CLAUDE.md](CLAUDE.md)).

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

## Roadmap — Vertical Slices

Each agent is built, hardened, evaluated, deployed and given a UI **before the next agent starts**. A slice is not done until it passes end to end locally (see CLAUDE.md §g). Shared infrastructure is built once, in the first slice, and later slices only add their delta. Specs 006+ are provisional until drafted; the numbering below supersedes the original 17-spec plan (older build logs use the old numbers).

### Foundation (done)

| Spec | Title | Status |
|------|-------|--------|
| 001 | Foundation: `pyproject`, config, Pydantic schemas, DB + pgvector | Done |
| 002 | Tenant-scoped data layer & migrations | Done |
| 003 | FastAPI skeleton + ARQ enqueue/worker plumbing | Done |

### Slice 1 — Lead Research (build, then harden, evaluate, deploy, UI)

| Step | Spec | Title | Status |
|------|------|-------|--------|
| Build | 004 | ICP segments, versioning & deterministic scoring | Done |
| Build | 005 | Lead Research agent with ICP evaluation (LangGraph) | Done |
| Harden (shared) | 006 | Prompt-injection guardrails library, per-tenant rate limits and cost quotas, log/trace redaction; applied to Lead Research | Done |
| Evaluate (shared) | 007 | Eval harness: golden datasets, adversarial corpus, labelled calibration set, LangSmith evals | Done |
| Deploy: local | 008 | Docker images + `docker compose` full stack (api, worker, migrate, Postgres, Redis) | Done |
| Deploy: local | 009 | Observability: structured logs, metrics (queue depth, job latency, LLM cost estimate), LangSmith run correlation, optional Prometheus + Grafana (OpenTelemetry deferred) | Done |
| Deploy: local | 010 | Local Kubernetes (kind): manifests + KEDA queue-depth autoscaling (0 to 5 workers), drain on scale-down, stale-job reaper, runs on the laptop | Done |
| Deploy: cloud sim | 011 | Terraform (VPC, EKS, RDS, ElastiCache, IRSA) written and validated with `terraform plan` only; **no apply, no cost** | Planned |
| UI | 012 | Streamlit: ICP editor, lead list, evaluation view | Planned |

### Slice 2 — CRM Hygiene

| Step | Spec | Title | Status |
|------|------|-------|--------|
| Build | 013 | Conflict detection (`ConflictItem`) | Planned |
| Build | 014 | HITL: LangGraph `interrupt` + `PostgresSaver` resume | Planned |
| Build | 015 | CRM connectors + OAuth token vault | Planned |
| Build | 016 | Webhook ingestion: signatures, idempotency, rate limits | Planned |
| Harden + evaluate | 017 | CRM-specific guardrails (write gating by risk, HITL policy) + golden datasets | Planned |
| Deploy + UI | 018 | Deploy delta (worker, scaler) + approval review queue | Planned |

### Slice 3 — LLM Wiki

| Step | Spec | Title | Status |
|------|------|-------|--------|
| Build | 019 | pgvector ingest + retrieval with mandatory `tenant_id` filter | Planned |
| Harden + evaluate | 020 | Retrieval isolation and poisoning tests, citation grounding, golden datasets | Planned |
| Deploy + UI | 021 | Deploy delta + wiki views | Planned |

### Slice 4 — Sales-to-Implementation Handoff

| Step | Spec | Title | Status |
|------|------|-------|--------|
| Build | 022 | Handoff agent (`HandoffBrief` from deal state + wiki + research) | Planned |
| Harden + evaluate | 023 | Faithfulness checks (no invented commitments), golden datasets | Planned |
| Deploy + UI | 024 | Deploy delta + handoff views | Planned |

### Real cloud deployment (later, optional)
After the local stack and the Terraform plan are validated, a real `terraform apply` to AWS can be added as a separate spec when you choose to spend the money.

## Build Progress

| Date | Day | Spec | Gate 1 | Gate 2 | Gate 3 | Log |
|------|-----|------|--------|--------|--------|-----|
| 2026-11-20 | 01 | 001 | PASS (16) | N/A | PASS | [log](docs/build-logs/2026-11-20-day-01.md) |
| 2026-11-20 | 02 | 002 | PASS (27) | N/A | PASS | [log](docs/build-logs/2026-11-20-day-02.md) |
| 2026-11-20 | 03 | 003 | PASS (59) | N/A | PASS | [log](docs/build-logs/2026-11-20-day-03.md) |
| 2026-11-20 | 04 | 004 | PASS (143) | N/A | PASS | [log](docs/build-logs/2026-11-20-day-04.md) |
| 2026-11-20 | 05 | 005 | PASS (243) | PASS (17 golden) | PASS live 2026-11-22 (calibration not run) | [log](docs/build-logs/2026-11-22-day-06.md) |
| 2026-11-23 | 07 | 006 | PASS (297) | PASS (22 golden + adversarial) | PASS live 2026-11-23 | [log](docs/build-logs/2026-11-23-day-07.md) |
| 2026-11-23 | 08 | 007 | PASS (347) | PASS (22 golden scripted + 28 adversarial + 10 judgement frozen) | PASS live 2026-11-23 (calibration not run) | [log](docs/build-logs/2026-11-23-day-08.md) |
| 2026-11-23 | 09 | 008 | PASS (367) | N/A (golden 22/22 in the image as evidence) | PASS live 2026-11-23 (31/31 smoke checks) | [log](docs/build-logs/2026-11-23-day-09.md) |
| 2026-11-23 | 10 | 009 | PASS (389) | N/A (golden 22/22 unchanged as evidence) | PASS live 2026-11-23 (29/29 observability smoke; 31/31 compose smoke re-run) | [log](docs/build-logs/2026-11-23-day-10.md) |
| 2026-11-23 | 11 | 010 | PASS (418) | N/A (golden 22/22 in the cluster image as evidence) | PASS live 2026-11-23 (39/39 kind smoke; compose 31/31 and observability 30/30 re-run) | [log](docs/build-logs/2026-11-23-day-11.md) |

## Workflow

1. Say **"Draft Spec XXX"** → spec appears in `specs/` for review.
2. Approve → implement → run 3-Gate Verification → write build log → update table above.
