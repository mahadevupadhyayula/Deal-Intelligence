"""ICP logic package (pure, I/O-free).

WHAT: Namespace for ICP scoring.
WHERE: Used by the Lead Research agent (Spec 005) and tests; no DB/LLM dependencies.
WHY: Keeping scoring free of I/O makes it deterministic, trivially testable, and safe to
re-run on stored verdicts to audit a stored score.
"""
