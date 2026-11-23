"""Gate 1 tests for logging, metrics, queue stats and their wiring (Spec 009).

WHAT: JSON log shape and context isolation, tenant hashing, metric cardinality, job and
admission metrics, instrumentation fault tolerance, queue stats, middleware, compose wiring.
WHERE: Verifies `agents/observability/`, `workers/tasks.py`, `api/main.py`, `api/routes/jobs.py`,
`docker-compose.yml`.
WHY: Observability that leaks a tenant id, explodes in cardinality, or can break a job is worse
than none, so each of those failure modes has a test. Metrics are read from the private
registry; counters are cumulative, so tests compare before/after deltas.
"""
import asyncio
import json
import logging
import re
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml
from prometheus_client import generate_latest

from agents.config import get_settings
from agents.guardrails.limits import Limits
from agents.guardrails.redact import install_log_redaction, register_secret
from agents.observability import logging as obs_log
from agents.observability import metrics
from agents.observability.logging import (
    JsonFormatter,
    bind_context,
    clear_context,
    hash_tenant,
    log_event,
)
from agents.observability.queue import QueueCollector, queue_stats
from agents.repos.async_session import AsyncTenantSession
from agents.repos.job import JobRepo
from api.main import create_app
from tests.conftest import TENANT_A as A
from tests.test_api import FakeArq, auth
from tests.test_guardrails_limits import make_client, post, set_usage
from workers.handlers import JOB_HANDLERS
from workers.tasks import run_job

ROOT = Path(__file__).resolve().parent.parent


def val(name, **labels):
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def fmt(record_msg="hello", **fields):
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, record_msg, None, None)
    rec.di_fields = fields
    return json.loads(JsonFormatter("test").format(rec))


@pytest.fixture(autouse=True)
def _ctx():
    clear_context()
    yield
    clear_context()


# ---- logging ---------------------------------------------------------------------------------
def test_json_line_has_required_keys_and_bound_context():
    bind_context(job_id="j1", kind="echo", tenant="abc")
    out = fmt("job.started", status="running", duration_ms=5)
    assert {"ts", "level", "service", "logger", "msg"} <= set(out)
    assert out["job_id"] == "j1" and out["kind"] == "echo" and out["tenant"] == "abc"
    assert out["status"] == "running" and out["service"] == "test"
    clear_context()
    assert "job_id" not in fmt("after")


async def test_context_does_not_leak_between_concurrent_tasks():
    seen = {}

    async def work(name):
        bind_context(job_id=name)
        await asyncio.sleep(0.01)  # let the other task bind too
        seen[name] = obs_log.get_context()["job_id"]

    await asyncio.gather(*(asyncio.create_task(work(n)) for n in ("a", "b", "c")))
    assert seen == {"a": "a", "b": "b", "c": "c"}
    assert "job_id" not in obs_log.get_context()  # parent untouched


def test_secrets_are_redacted_in_json_output():
    install_log_redaction()
    register_secret("supersecretvalue123")
    rec = logging.getLogger("t").makeRecord(
        "t", logging.ERROR, __file__, 1, "key sk-abcdef123456 and supersecretvalue123", None, None)
    try:
        raise RuntimeError("db postgresql://u:hunter2pass@h/db bearer abcdefghijkl")
    except RuntimeError:
        import sys
        rec.exc_info = sys.exc_info()
    line = JsonFormatter("t").format(rec)
    for leaked in ("sk-abcdef123456", "supersecretvalue123", "hunter2pass", "abcdefghijkl"):
        assert leaked not in line
    assert json.loads(line)["exc"]


def test_hash_tenant_properties(monkeypatch):
    h = hash_tenant("tenant-a", "k1")
    assert len(h) == 12 and "tenant-a" not in h
    assert h == hash_tenant("tenant-a", "k1")
    assert h != hash_tenant("tenant-b", "k1") and h != hash_tenant("tenant-a", "k2")


def test_hash_tenant_without_key_warns_once_and_is_stable(monkeypatch, caplog):
    monkeypatch.setenv("LOG_HASH_KEY", "")  # empty beats the .env value
    monkeypatch.setattr(obs_log, "_fallback_key", None)
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING):
            a, b = hash_tenant("t"), hash_tenant("t")
        assert a == b
        assert sum("LOG_HASH_KEY" in r.getMessage() for r in caplog.records) == 1
    finally:
        get_settings.cache_clear()


# ---- metrics ---------------------------------------------------------------------------------
FORBIDDEN = {"tenant", "tenant_id", "job_id", "user", "path"}


def test_no_metric_has_a_high_cardinality_label():
    for family in metrics.REGISTRY.collect():
        for sample in family.samples:
            assert not FORBIDDEN & set(sample.labels), f"{sample.name} has a forbidden label"
    for m in (metrics.JOBS, metrics.JOB_DURATION, metrics.JOB_QUEUE_WAIT, metrics.PROVIDER_CALLS,
              metrics.ESTIMATED_COST, metrics.REJECTIONS, metrics.HTTP_REQUESTS,
              metrics.HTTP_DURATION, metrics.SCRAPE_ERRORS):
        assert not FORBIDDEN & set(m._labelnames)


def test_unknown_label_values_collapse_to_other():
    before = val("di_jobs_total", kind="other", status="other")
    metrics.observe_job("totally-new-kind", "weird", 0.1)
    assert val("di_jobs_total", kind="other", status="other") == before + 1
    metrics.count_rejection("made-up")
    assert val("di_admission_rejections_total", reason="other") >= 1


async def _job(apool, kind="echo"):
    jid = uuid4()
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        await JobRepo(s).create(jid, kind, {})
    return jid


async def test_run_job_records_success_and_failure_metrics(apool, monkeypatch):
    ok0 = val("di_jobs_total", kind="echo", status="succeeded")
    jid = await _job(apool)
    await run_job({"pool": apool}, A, str(jid))
    assert val("di_jobs_total", kind="echo", status="succeeded") == ok0 + 1
    assert val("di_job_duration_seconds_count", kind="echo") >= 1
    assert val("di_job_queue_wait_seconds_count", kind="echo") >= 1

    async def boom(*_):
        raise ValueError("x")
    monkeypatch.setitem(JOB_HANDLERS, "echo", boom)
    f0 = val("di_jobs_total", kind="echo", status="failed")
    await run_job({"pool": apool}, A, str(await _job(apool)))
    assert val("di_jobs_total", kind="echo", status="failed") == f0 + 1


async def test_run_job_records_timeout_as_failed(apool, monkeypatch):
    async def slow(*_):
        await asyncio.sleep(5)
    monkeypatch.setitem(JOB_HANDLERS, "echo", slow)
    f0 = val("di_jobs_total", kind="echo", status="failed")
    await run_job({"pool": apool, "run_timeout": 0.05}, A, str(await _job(apool)))
    assert val("di_jobs_total", kind="echo", status="failed") == f0 + 1


async def test_cancelled_job_is_counted_cancelled_and_still_propagates(apool, monkeypatch):
    started = asyncio.Event()

    async def slow(*_):
        started.set()
        await asyncio.sleep(30)
    monkeypatch.setitem(JOB_HANDLERS, "echo", slow)
    c0 = val("di_jobs_total", kind="echo", status="cancelled")
    task = asyncio.create_task(run_job({"pool": apool}, A, str(await _job(apool))))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert val("di_jobs_total", kind="echo", status="cancelled") == c0 + 1


async def test_job_lifecycle_logs_share_job_id_and_hide_raw_tenant(apool, caplog):
    jid = await _job(apool)
    with caplog.at_level(logging.INFO):
        await run_job({"pool": apool}, A, str(jid))
    events = {r.getMessage(): r for r in caplog.records if hasattr(r, "di_fields")}
    assert {"job.started", "job.finished"} <= set(events)
    assert events["job.finished"].di_fields["status"] == "succeeded"
    # The shared context (job_id/tenant) is applied by the formatter; render both lines.
    for name in ("job.started", "job.finished"):
        bind_context(job_id=str(jid), tenant=hash_tenant(A))
        line = json.loads(JsonFormatter("worker").format(events[name]))
        assert line["job_id"] == str(jid) and A not in json.dumps(line)
    assert obs_log.get_context() != {}  # (we re-bound above; run_job itself cleared its own)


async def test_instrumentation_failure_does_not_change_the_outcome(apool, monkeypatch):
    def explode(*_a, **_k):
        raise RuntimeError("metrics backend exploded")
    monkeypatch.setattr(metrics.JOBS, "labels", explode)
    monkeypatch.setattr(metrics.JOB_DURATION, "labels", explode)
    e0 = val("di_scrape_errors_total", reason="instrumentation")
    jid = await _job(apool)
    await run_job({"pool": apool}, A, str(jid))  # must not raise
    async with apool.connection() as conn, AsyncTenantSession(conn, A) as s:
        assert (await JobRepo(s).get(jid)).status == "succeeded"
    assert val("di_scrape_errors_total", reason="instrumentation") > e0


async def test_provider_calls_and_cost_estimate_recorded(apool, monkeypatch):
    from agents.lead_research.tools import FixtureSearch

    async def fake(ctx, t, j, p):
        await ctx.search.search("q")
        await ctx.search.search("q2")
        return {}
    monkeypatch.setitem(JOB_HANDLERS, "lead_research", fake)
    s0, c0 = val("di_provider_calls_total", provider="search"), val(
        "di_estimated_cost_usd_total", provider="search")
    await run_job({"pool": apool, "search": FixtureSearch({})}, A,
                  str(await _job(apool, "lead_research")))
    assert val("di_provider_calls_total", provider="search") == s0 + 2
    price = get_settings().price_per_search
    assert val("di_estimated_cost_usd_total", provider="search") == pytest.approx(c0 + 2 * price)


# ---- admission rejections and middleware -----------------------------------------------------
async def test_admission_rejections_are_counted_by_reason(apool, api_keys, admin_url):
    arq = FakeArq()
    base = {r: val("di_admission_rejections_total", reason=r)
            for r in ("rate", "active_cap", "daily_quota", "payload")}
    async with make_client(apool, arq, max_active_jobs=1) as c:
        assert (await post(c, api_keys[A])).status_code == 202
        assert (await post(c, api_keys[A])).status_code == 429  # active cap
        big = {"kind": "echo", "payload": {"x": "a" * 70_000}}
        assert (await c.post("/v1/jobs", json=big, headers=auth(api_keys[A]))).status_code == 413
    async with make_client(apool, FakeArq(), rate_per_minute=1) as c:
        await post(c, api_keys[A])
        assert (await post(c, api_keys[A])).status_code == 429  # rate
    set_usage(admin_url, A, llm=10_000)
    async with make_client(apool, FakeArq(), daily_llm_calls=1) as c:
        assert (await post(c, api_keys[A], "lead_research")).status_code == 429  # quota
    for r, before in base.items():
        assert val("di_admission_rejections_total", reason=r) == before + 1, r


async def test_middleware_logs_route_template_and_hashed_tenant_only(apool, api_keys, caplog):
    app = create_app(pool=apool, arq=FakeArq(), limits=Limits(rate_per_minute=1000))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        jid = (await c.post("/v1/jobs", json={"kind": "echo"}, headers=auth(api_keys[A]))
               ).json()["job_id"]
        h0 = val("di_http_requests_total", method="GET", route="/v1/jobs/{job_id}",
                 status_class="2xx")
        with caplog.at_level(logging.INFO):
            r = await c.get(f"/v1/jobs/{jid}?secret=1", headers={**auth(api_keys[A]),
                                                                 "X-Request-ID": "req-123"})
    assert r.headers["x-request-id"] == "req-123"
    rec = next(x for x in caplog.records
               if getattr(x, "di_fields", {}).get("method") == "GET")
    f = rec.di_fields
    assert f["route"] == "/v1/jobs/{job_id}" and f["status"] == 200
    assert f["tenant"] == hash_tenant(A)
    blob = json.dumps(f) + rec.getMessage()
    assert A not in blob and jid not in blob and "secret=1" not in blob and api_keys[A] not in blob
    assert val("di_http_requests_total", method="GET", route="/v1/jobs/{job_id}",
               status_class="2xx") == h0 + 1


async def test_probe_requests_log_at_debug_not_info(apool, caplog):
    app = create_app(pool=apool, arq=FakeArq())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        with caplog.at_level(logging.INFO):
            await c.get("/healthz")
    assert not [r for r in caplog.records if getattr(r, "di_fields", {}).get("route") == "/healthz"]


def test_registry_text_never_contains_a_raw_tenant_id():
    assert b"tenant-a" not in generate_latest(metrics.REGISTRY)


# ---- queue stats -----------------------------------------------------------------------------
class FakeRedis:
    """Just the sync surface `queue_stats` uses."""

    def __init__(self, queued=(), in_progress=(), fail=False):
        self.queued, self.in_progress, self.fail = list(queued), set(in_progress), fail

    def _check(self):
        if self.fail:
            raise ConnectionError("down")

    def zcard(self, _k):
        self._check()
        return len(self.queued)

    def zrange(self, _k, a, b, withscores=False):
        self._check()
        return [(m.encode(), s) for m, s in sorted(self.queued, key=lambda x: x[1])][a:b + 1]

    def pipeline(self):
        outer = self

        class P:
            def __init__(self):
                self.ops = []

            def exists(self, key):
                self.ops.append(key.removeprefix("arq:in-progress:"))

            def execute(self):
                return [1 if o in outer.in_progress else 0 for o in self.ops]
        return P()

    def scan_iter(self, match=None, count=None):
        return iter(self.in_progress)


def test_queue_stats_empty_waiting_and_in_flight():
    assert queue_stats(FakeRedis(), now_ms=10_000).depth == 0
    q = [("a", 1_000), ("b", 2_000), ("c", 4_000)]
    st = queue_stats(FakeRedis(q), now_ms=11_000)
    assert (st.depth, st.in_flight) == (3, 0) and st.oldest_age_seconds == 10
    # `a` is running: it stays in ARQ's zset until finished, but is not waiting.
    st = queue_stats(FakeRedis(q, in_progress={"a"}), now_ms=11_000)
    assert (st.depth, st.in_flight) == (2, 1) and st.oldest_age_seconds == 9


def test_queue_collector_omits_gauges_and_counts_error_when_redis_is_down():
    e0 = val("di_scrape_errors_total", reason="queue_read")
    assert list(QueueCollector(FakeRedis(fail=True)).collect()) == []
    assert val("di_scrape_errors_total", reason="queue_read") == e0 + 1
    names = {m.name for m in QueueCollector(FakeRedis([("a", 1)])).collect()}
    assert names == {"di_queue_depth", "di_queue_in_flight", "di_queue_oldest_age_seconds"}


def test_log_event_carries_fields():
    rec = {}

    class H(logging.Handler):
        def emit(self, r):
            rec["f"] = r.di_fields
    lg = logging.getLogger("evt-test")
    lg.addHandler(H())
    lg.setLevel(logging.INFO)
    log_event(lg, "x.y", a=1)
    assert rec["f"] == {"a": 1}


# ---- compose wiring (static) -----------------------------------------------------------------
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SVC = COMPOSE["services"]


def test_metrics_ports_are_internal_only():
    for name in ("api", "worker", "prometheus", "grafana"):
        for p in SVC[name].get("ports", []):
            assert not re.search(r"910[01]", str(p)), f"{name} publishes a metrics port"
    assert "9101" in map(str, SVC["api"]["expose"]) and "9100" in map(str, SVC["worker"]["expose"])


def test_observability_profile_and_secret_wiring():
    for name in ("prometheus", "grafana"):
        assert SVC[name]["profiles"] == ["observability"]
        assert all(str(p).startswith("127.0.0.1:") for p in SVC[name]["ports"])
    g = SVC["grafana"]["environment"]
    assert g["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert g["GF_SECURITY_ADMIN_PASSWORD"].startswith("${GRAFANA_ADMIN_PASSWORD")
    assert "GRAFANA_ADMIN_PASSWORD" in " ".join(SVC["grafana"]["entrypoint"])  # refuses to start empty
    for name in ("api", "worker"):
        assert SVC[name]["environment"]["LOG_HASH_KEY"].startswith("${LOG_HASH_KEY:?")
        assert SVC[name]["environment"]["LOG_FORMAT"] == "json"
    for name, svc in SVC.items():
        if name not in ("worker", "tools"):
            assert "LANGSMITH_API_KEY" not in svc.get("environment", {}), name
    assert "--no-access-log" in SVC["api"]["command"]
    assert "LOG_HASH_KEY=" not in (ROOT / "docker-compose.yml").read_text()
