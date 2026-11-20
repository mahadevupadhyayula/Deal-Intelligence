# Spec XXX — <Title>

**Status:** Draft | Approved | In Progress | Done
**Phase:** <n> · **Depends on:** <spec ids>

## Objective
What this spec delivers and why, in 2–4 sentences. Where it fits in the system.

## Technical Requirements
- Files to create/modify (paths)
- Interfaces, models, schemas, env vars
- Constraints from CLAUDE.md that apply (tenant_id, no blocking, no secrets, commenting rule)

## Out of Scope
- Explicitly deferred items and the spec that will handle them

## 3-Gate Verification & Acceptance Criteria

### Gate 1 — Unit (`pytest -v`)
- [ ] <unit / Pydantic validation tests>
- [ ] `tenant_id` isolation tests
### Gate 2 — Agent Behavior (agent specs only; else mark N/A with reason)
- [ ] Trajectory/interrupts match `tests/fixtures/` golden datasets
- [ ] LangSmith trace reviewed
### Gate 3 — Live Smoke Test
- [ ] <exact command: `psql` / `curl` / `docker compose` / `terraform plan`> → expected result

### Definition of Done
- [ ] All gates pass; Verification Report (Pass/Fail) output
- [ ] Every file has WHAT/WHERE/WHY docstring
- [ ] Build log written; README progress table updated
