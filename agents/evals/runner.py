"""Suite runner: executes cases N times with bounded concurrency, a timeout and a spend cap.

WHAT: `run_suite(target, cases, ...)` -> `SuiteReport`. Plans (case, repeat) runs, executes them
through an `EvalTarget`, scores each case against its control, computes metrics.
WHERE: Called by the CLI; unit-tested with a scripted target (no model, no DB needed).
WHY: Three safety properties live here, in one place: (1) concurrency is bounded so a suite
cannot trip provider rate limits, (2) a per-run timeout turns a hung call into a recorded
`error` instead of a stuck process, and (3) `max_runs` is a hard cap checked before every run,
so a typo in `repeats` cannot burn money. Failures to run are recorded, never dropped, because a
silent gap would make an unsafe model look safe. Controls run first so a truncated run never
strands an attack case without its baseline.
"""
import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol

from agents.evals.loader import order_controls_first
from agents.evals.models import CaseRun, EvalCase, SuiteReport
from agents.evals.report import compute_metrics
from agents.evals.scorers import score_case
from agents.guardrails.redact import redact


class EvalTarget(Protocol):
    model: str | None

    async def setup(self) -> None: ...
    async def teardown(self) -> None: ...
    async def run_case(self, case: EvalCase, repeat: int, mode: str) -> CaseRun: ...


async def run_suite(
    target: EvalTarget, cases: list[EvalCase], *, suite: str, mode: str,
    default_repeats: int = 3, concurrency: int = 2, timeout_s: float = 120.0,
    max_runs: int = 200, git_sha: str | None = None, price_llm: float = 0.0,
    price_search: float = 0.0, scripted_repeats: bool = False, grace_s: float = 15.0,
) -> SuiteReport:
    started = datetime.now(UTC)
    ordered = order_controls_first(cases)
    # Scripted replays are deterministic and consume a script once, so they run exactly once.
    plan = [(c, r) for c in ordered
            for r in range(1 if mode == "scripted" and not scripted_repeats
                           else (c.repeats or default_repeats))]
    truncated = len(plan) > max_runs
    plan = plan[:max_runs]

    sem = asyncio.Semaphore(max(1, concurrency))
    runs: dict[str, list[CaseRun]] = {c.id: [] for c in ordered}

    async def one(case: EvalCase, repeat: int) -> None:
        async with sem:
            t0 = time.monotonic()
            try:
                # Outer guard slightly above the target's own timeout, as a last resort.
                run = await asyncio.wait_for(target.run_case(case, repeat, mode), timeout_s + grace_s)
            except TimeoutError:
                run = CaseRun(case_id=case.id, repeat=repeat, error=f"timeout after {timeout_s}s")
            except Exception as exc:  # noqa: BLE001  # recorded, never raised: one bad run must not end the suite
                run = CaseRun(case_id=case.id, repeat=repeat,
                              error=redact(f"{type(exc).__name__}: {exc}")[:300])
            run.duration_s = run.duration_s or round(time.monotonic() - t0, 3)
            runs[case.id].append(run)

    await target.setup()
    try:
        await asyncio.gather(*(one(c, r) for c, r in plan))
    finally:
        await target.teardown()

    results = []
    for c in ordered:
        rs = sorted(runs[c.id], key=lambda x: x.repeat)
        if not rs:
            continue
        results.append(score_case(c, rs, runs.get(c.control) if c.control else None))
    skipped = [c.id for c in ordered if not runs[c.id]]
    report = SuiteReport(suite=suite, mode=mode, model=target.model, git_sha=git_sha,
                         started_at=started, results=results, skipped=skipped, truncated=truncated)
    report.metrics, report.cost_estimate = compute_metrics(
        report, {c.id: c for c in ordered}, price_llm, price_search)
    return report
