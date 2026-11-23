# Architecture

## Glossary: tenant vs. lead

`tenant_id` is the **account that owns a row**; it is an isolation and access-control key, not a description of a company. Leads, ICP segments, research results and ICP evaluations are all data *owned by* a tenant and carry that tenant's `tenant_id`. A lead's own identity is separate (`lead_id`, domain, company name). A lead is never a tenant. If a lead converts into a customer, it becomes a **new** tenant with its own `tenant_id`; the owning tenant's lead row links to it through a separate field (e.g. `converted_tenant_id`), so one tenant's data never mixes with another's.

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
3. **Webhook idempotency & rate limits:** signature verification, idempotency key per event stored before enqueue, per-tenant rate limiting; handlers return fast and enqueue. Per-tenant job rate limit, active-job cap and daily provider quotas are built (Spec 006, `agents/guardrails/limits.py`); webhook signatures/idempotency come with Spec 016.
4. **OAuth token vault:** CRM tokens encrypted (KMS / Secrets Manager), referenced by id, never logged or placed in agent state/prompts; refresh handled centrally.
5. **Prompt-injection guardrails:** all external text (emails, web pages, CRM notes) is treated as untrusted data; delimited in prompts, tool permissions least-privilege, outputs schema-validated by Pydantic, writes to CRM gated by HITL for high risk. Shared implementation in `agents/guardrails/` (Spec 006): nonce-delimited untrusted text, sanitising, injection flags, evidence-overlap rule, canary leak detection, redaction of logs/errors/traces.

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

## 4. Delivery Model: Vertical Slices

Each agent ships as a slice: **build → harden → evaluate → deploy (local) → UI**, in the order Lead Research, CRM Hygiene, LLM Wiki, Sales-to-Implementation Handoff (Wiki precedes Handoff because Handoff reads it). See the README roadmap for spec numbers.

### Which guardrails are shared and which are per-agent

| Shared (built once, Slice 1, reused) | Per-agent (lives with the agent's specs) |
|---|---|
| Untrusted-content wrapping and delimiting; schema-validated outputs; tool allowlists | **Lead Research:** web-page injection cases, entity and domain checks, ICP calibration against labelled companies |
| Evidence/quote verification pattern (from Spec 005) | **CRM Hygiene:** write gating by risk, HITL policy, OAuth token vault, webhook signature verification |
| Per-tenant rate limits and cost quotas; LLM/search budgets | **LLM Wiki:** retrieval tenant-isolation and leakage tests, ingest poisoning, citation grounding |
| Log, trace and prompt redaction (secrets, PII); hashed tenant ids in traces | **Handoff:** faithfulness to sources, no invented commitments |
| Eval harness: dataset loader, scorers, LangSmith runner, adversarial corpus format | Agent-specific golden datasets and expected trajectories |
| Docker, Compose, kind + KEDA, Terraform modules, observability stack | Per-agent deploy delta (worker deployment, scaler, config) |

### Deployment path
1. Docker Compose full stack (Spec 008, **done**: one hardened image, `docker-compose.yml`, `scripts/compose_smoke.sh`, runbook in `docs/local-stack.md`) and observability (Spec 009, **done**: JSON logs correlated by `job_id` with hashed tenant ids, fleet-level Prometheus metrics incl. queue depth/oldest age for KEDA, optional Prometheus+Grafana profile, LangSmith run metadata; see `docs/local-stack.md`).
2. Local Kubernetes with KEDA on a laptop (Spec 010): no cloud cost.
3. Terraform validated with `terraform plan` only (Spec 011): the AWS design above is written and checked, not applied.
4. A real AWS `apply` is a separate, optional spec once the owner chooses to spend.
