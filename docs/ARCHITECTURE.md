# Architecture

## 1. Core Agents

All agents are LangGraph graphs run inside ARQ workers. Every run is keyed by `thread_id = "{tenant_id}:{deal_id}:{run_id}"`.

### Lead Research
- **Input:** company/contact seed from CRM or UI. **Output:** `LeadProfile` (firmographics, signals, sources).
- Tool-using graph (search, enrichment) with source citations; results written tenant-scoped.

### CRM Hygiene (with HITL)
- Compares CRM fields against evidence (calls, emails, research) and emits `ConflictItem`s.
- Low-risk fixes auto-apply; high-risk ones hit a LangGraph `interrupt`. State is persisted by `PostgresSaver`, so a human can approve hours later and **any** worker resumes the thread (stateless HITL).

### LLM Wiki
- Maintains a per-tenant knowledge base of deal/account facts: chunk → embed → `pgvector`. Every retrieval applies a mandatory `tenant_id` metadata filter.
- Serves as grounded context for the other three agents.

### Sales-to-Implementation Handoff
- On closed-won, synthesizes `DealState` + wiki + research into a `HandoffBrief` (commitments, stakeholders, risks, open questions) for the implementation team.

## 2. Five B2B Production Guardrails

1. **Multi-tenancy:** `tenant_id` required on all models, queries, vector filters, and thread ids; isolation tests are part of Gate 1. Postgres row-level security as defense in depth.
2. **Stateless HITL:** pause/resume via `PostgresSaver`; no worker-local state.
3. **Webhook idempotency & rate limits:** signature verification, idempotency key per event stored before enqueue, per-tenant rate limiting; handlers return fast and enqueue.
4. **OAuth token vault:** CRM tokens encrypted (KMS / Secrets Manager), referenced by id, never logged or placed in agent state/prompts; refresh handled centrally.
5. **Prompt-injection guardrails:** all external text (emails, web pages, CRM notes) is treated as untrusted data; delimited in prompts, tool permissions least-privilege, outputs schema-validated by Pydantic, writes to CRM gated by HITL for high risk.

## 3. Target AWS Architecture (EKS + Terraform)

```
Route53/ALB ─▶ EKS: api (FastAPI, HPA)
                    workers (ARQ + LangGraph, KEDA ScaledObject on Redis queue depth)
                    frontend (Streamlit)
            ├─ RDS PostgreSQL (pgvector, PostgresSaver tables)
            ├─ ElastiCache Redis (ARQ)  [SQS optional]
            ├─ Secrets Manager + KMS (OAuth vault, API keys) via IRSA
            ├─ ECR (images)   └─ CloudWatch + LangSmith (observability)
```

- Terraform in `infra/terraform/` (VPC, EKS, RDS, ElastiCache, IAM/IRSA); manifests in `infra/k8s/`; images in `infra/docker/`.
- KEDA scales worker pods on queue depth, down to near-zero when idle.
