"""Queue backlog measurement from ARQ's Redis keys.

WHAT: `queue_stats(redis)` -> depth (waiting), in_flight, oldest waiting age; `QueueCollector`
exposes them to Prometheus at scrape time.
WHERE: Registered on the API's metrics listener (`api/main.py`); Spec 010's KEDA scaler reads
the same Redis key.
WHY: ARQ keeps a job in the sorted set `arq:queue` until it FINISHES, so ZCARD counts waiting
plus running jobs. We subtract jobs that have an `arq:in-progress:<id>` marker to report the
true backlog, and report in-flight separately (KEDA may prefer the total). The score is the
enqueue time in ms, so the first waiting entry gives the oldest age; depth alone cannot tell
"busy" from "stuck". The collector runs on the metrics listener THREAD with a short-timeout sync
client, so a slow Redis can never block the FastAPI event loop; failure omits the gauges and
counts a scrape error instead of raising.
"""
import time
from dataclasses import dataclass

from prometheus_client.core import GaugeMetricFamily

from agents.observability.metrics import count_scrape_error

QUEUE_KEY = "arq:queue"
IN_PROGRESS_PREFIX = "arq:in-progress:"
_SCAN_WINDOW = 100  # oldest-age looks at the first N entries only (bounded work per scrape)


@dataclass(frozen=True)
class QueueStats:
    depth: int
    in_flight: int
    oldest_age_seconds: float

    @property
    def outstanding(self) -> int:
        """Waiting + running: the signal KEDA scales workers on (Spec 010)."""
        return self.depth + self.in_flight


def queue_stats(redis, now_ms: float | None = None) -> QueueStats:
    """`redis` is a sync client (decode_responses=False or True both work)."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    total = redis.zcard(QUEUE_KEY)
    head = redis.zrange(QUEUE_KEY, 0, _SCAN_WINDOW - 1, withscores=True)
    pipe = redis.pipeline()
    for member, _ in head:
        mid = member.decode() if isinstance(member, bytes) else member
        pipe.exists(IN_PROGRESS_PREFIX + mid)
    running_flags = pipe.execute() if head else []
    oldest = 0.0
    for (_, score), running in zip(head, running_flags, strict=True):
        if not running:
            oldest = max(0.0, (now_ms - score) / 1000)
            break
    in_flight = sum(1 for r in running_flags if r)
    if total > len(head):  # more than one window: count in-progress markers exactly
        in_flight = sum(1 for _ in redis.scan_iter(match=IN_PROGRESS_PREFIX + "*", count=500))
    return QueueStats(depth=max(0, total - in_flight), in_flight=in_flight, oldest_age_seconds=oldest)


class QueueCollector:
    """prometheus_client custom collector; `collect()` runs per scrape on the listener thread."""

    def __init__(self, client):
        self.client = client

    def collect(self):
        try:
            st = queue_stats(self.client)
        except Exception:  # noqa: BLE001 - any Redis problem: omit gauges, count it
            count_scrape_error("queue_read")
            return
        for name, doc, val in (
            ("di_queue_depth", "Jobs waiting for a worker", st.depth),
            ("di_queue_in_flight", "Jobs currently running", st.in_flight),
            ("di_queue_outstanding", "Waiting plus running jobs (KEDA scaling signal)",
             st.outstanding),
            ("di_queue_oldest_age_seconds", "Age of the oldest waiting job", st.oldest_age_seconds),
        ):
            g = GaugeMetricFamily(name, doc)
            g.add_metric([], val)
            yield g
