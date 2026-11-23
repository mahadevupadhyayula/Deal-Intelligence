"""Eval target adapters (one per agent).

WHAT: Package for `EvalTarget` implementations; each wires one agent's real graph for evals.
WHERE: Used by `agents/evals/cli.py`.
WHY: The core must not know any agent's internals; an adapter is the only agent-specific code.
"""
