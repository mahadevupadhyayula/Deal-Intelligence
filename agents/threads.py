"""Tenant-safe LangGraph thread ids.

WHAT: Builds/parses `"tenant:deal:run"` thread ids.
WHERE: Used by workers when invoking LangGraph graphs with `PostgresSaver` (Spec 008+).
WHY: Checkpoints are keyed by thread_id; embedding tenant_id in it means a resumed
thread can always be attributed to (and verified against) its tenant. Forbidding ':' in
parts makes parsing unambiguous and prevents one tenant forging another's prefix.
"""


def make_thread_id(tenant_id: str, deal_id: str, run_id: str) -> str:
    parts = (tenant_id, deal_id, run_id)
    for p in parts:
        if not p or not p.strip():
            raise ValueError("thread id parts must be non-empty")
        if ":" in p:
            raise ValueError("thread id parts must not contain ':'")
    return ":".join(parts)


def parse_thread_id(thread_id: str) -> tuple[str, str, str]:
    parts = thread_id.split(":")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise ValueError(f"malformed thread id: {thread_id!r}")
    return parts[0], parts[1], parts[2]
