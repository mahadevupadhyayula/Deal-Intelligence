"""Eval harness package (Spec 007).

WHAT: Shared dataset/scorer/runner/report machinery for evaluating agents with a real model.
WHERE: Offline tooling run from the CLI (`python -m agents.evals`); imports nothing from `api/`.
WHY: Scripted tests prove orchestration; only runs against a real model can show whether the
agent judges well and resists attacks. Keeping the core agent-agnostic lets later slices add
only an adapter and datasets.
"""
