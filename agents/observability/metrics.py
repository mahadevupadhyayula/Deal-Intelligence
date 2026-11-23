"""Prometheus metric definitions and safe recording helpers.

WHAT: Every `di_*` metric in one module, a private registry, and helpers that record them.
WHERE: Used by `run_job` (worker), the API middleware/routes, and `queue.py`.
WHY: One module keeps names and labels reviewable and lets a test enforce the cardinality
rule. NO metric is labelled by tenant (unbounded cardinality, and a scrape would reveal the
tenant list); labels come from small closed sets, and helpers map anything unknown to `other`.
A private `CollectorRegistry` (not the global default) avoids duplicate-registration errors in
tests and keeps Python/process collectors out unless added deliberately. Recording helpers
never raise: instrumentation must not be able to change a job outcome or fail a request.
"""
import logging

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from agents.job_kinds import JOB_KINDS

REGISTRY = CollectorRegistry()
_log = logging.getLogger(__name__)

_KINDS = frozenset(JOB_KINDS)
_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_PROVIDERS = frozenset({"llm", "search"})
_REASONS = frozenset({"rate", "active_cap", "daily_quota", "payload"})
_SCRAPE = frozenset({"queue_read", "instrumentation", "http", "reaper"})
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_STATUS_CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx"})
_DURATION_BUCKETS = (0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)
_HTTP_BUCKETS = (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)

JOBS = Counter("di_jobs_total", "Finished jobs", ["kind", "status"], registry=REGISTRY)
JOB_DURATION = Histogram("di_job_duration_seconds", "Handler wall time", ["kind"],
                         buckets=_DURATION_BUCKETS, registry=REGISTRY)
JOB_QUEUE_WAIT = Histogram("di_job_queue_wait_seconds", "Creation to start", ["kind"],
                           buckets=_DURATION_BUCKETS, registry=REGISTRY)
PROVIDER_CALLS = Counter("di_provider_calls_total", "Metered provider calls", ["provider"],
                         registry=REGISTRY)
ESTIMATED_COST = Counter("di_estimated_cost_usd_total", "Calls x configured price", ["provider"],
                         registry=REGISTRY)
REJECTIONS = Counter("di_admission_rejections_total", "Admission rejections", ["reason"],
                     registry=REGISTRY)
HTTP_REQUESTS = Counter("di_http_requests_total", "HTTP requests",
                        ["method", "route", "status_class"], registry=REGISTRY)
HTTP_DURATION = Histogram("di_http_request_duration_seconds", "HTTP latency",
                          ["method", "route"], buckets=_HTTP_BUCKETS, registry=REGISTRY)
SCRAPE_ERRORS = Counter("di_scrape_errors_total", "Instrumentation failures", ["reason"],
                        registry=REGISTRY)
REAPED = Counter("di_reaped_jobs_total", "Jobs failed by the stale-job reaper", registry=REGISTRY)
WORKER_UP = Gauge("di_worker_up", "1 while the worker process is alive", registry=REGISTRY)


def _one_of(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else "other"


def count_scrape_error(reason: str) -> None:
    try:
        SCRAPE_ERRORS.labels(_one_of(reason, _SCRAPE)).inc()
    except Exception:  # noqa: BLE001 - last line of defence: never raise from here
        _log.debug("could not count scrape error", exc_info=True)


def _safe(fn):
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - instrumentation must never break the caller
            count_scrape_error("instrumentation")
            _log.debug("metric recording failed", exc_info=True)
    wrapper.__name__ = fn.__name__
    return wrapper


@_safe
def observe_job(kind: str, status: str, seconds: float) -> None:
    kind = _one_of(kind, _KINDS)
    JOBS.labels(kind, _one_of(status, _STATUSES)).inc()
    JOB_DURATION.labels(kind).observe(max(0.0, seconds))


@_safe
def observe_queue_wait(kind: str, seconds: float) -> None:
    JOB_QUEUE_WAIT.labels(_one_of(kind, _KINDS)).observe(max(0.0, seconds))


@_safe
def count_provider_calls(provider: str, n: int, unit_price: float) -> None:
    provider = _one_of(provider, _PROVIDERS)
    if n > 0:
        PROVIDER_CALLS.labels(provider).inc(n)
        ESTIMATED_COST.labels(provider).inc(n * unit_price)


@_safe
def count_reaped() -> None:
    REAPED.inc()


@_safe
def count_rejection(reason: str) -> None:
    REJECTIONS.labels(_one_of(reason, _REASONS)).inc()


@_safe
def observe_http(method: str, route: str, status: int, seconds: float) -> None:
    method = _one_of(method.upper(), _METHODS)
    HTTP_REQUESTS.labels(method, route, _one_of(f"{status // 100}xx", _STATUS_CLASSES)).inc()
    HTTP_DURATION.labels(method, route).observe(max(0.0, seconds))


def start_metrics_server(port: int, host: str, extra_collectors=()):
    """Serve `REGISTRY` on a daemon thread (never the event loop). Returns the server or None."""
    from prometheus_client import start_http_server

    for c in extra_collectors:
        REGISTRY.register(c)
    try:
        server, _thread = start_http_server(port, addr=host, registry=REGISTRY)
        return server
    except OSError:
        _log.warning("metrics port %s unavailable; metrics disabled for this process", port)
        return None
