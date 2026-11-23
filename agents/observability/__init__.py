"""Observability: structured logs, Prometheus metrics, trace correlation (Spec 009).

WHAT: Package marker; see `logging.py`, `metrics.py`, `queue.py`, `tracing.py`.
WHERE: Used by the API (`api/main.py`) and the worker (`workers/`); nothing here touches the DB.
WHY: One place for "how the system explains itself", so agents stay free of logging/metrics code.
"""
