"""Shared guardrails used by every agent (Spec 006).

WHAT: Untrusted-content hardening, injection detection, redaction, tenant limits/quotas and
usage metering.
WHERE: Imported by agents (prompt building, validation), the API (admission) and the worker
(metering, quota re-check, error redaction). Contains no agent-specific logic.
WHY: Written once so Lead Research, CRM Hygiene, the Wiki and Handoff all get the same
controls instead of each re-implementing (and diverging on) them.
"""
