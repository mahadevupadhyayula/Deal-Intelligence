"""Eval CLI: `run` a suite, `compare` a report with its baseline.

WHAT: Argument parsing, preflight checks, wiring (settings -> target -> runner -> report ->
baseline -> optional LangSmith) and exit codes.
WHERE: Entry point `python -m agents.evals`; offline tooling, imports nothing from `api/`.
WHY: Exit codes make it scriptable: 0 ok, 1 a gating check failed (or the run was truncated),
2 a metric regressed against the committed baseline, 3 setup problem (missing keys/DB/dataset).
Preflight fails BEFORE any model call so a missing key never wastes a partial run. Secrets are
registered for redaction up front, so even an unexpected provider error cannot leak a key into a
report or the console.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

from agents.config import get_settings
from agents.evals import regression
from agents.evals.models import SuiteReport
from agents.evals.report import render_markdown, write_report
from agents.evals.runner import run_suite
from agents.evals.targets.lead_research import SUITE_MODES, LeadResearchTarget, load_suite
from agents.guardrails.redact import install_log_redaction, register_secret

BASELINES = Path(__file__).resolve().parents[2] / "evals" / "baselines"


def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5, check=True).stdout.strip() or None
    except Exception:  # noqa: BLE001  (git missing/not a repo: sha is optional metadata)
        return None


def _setup_error(msg: str) -> int:
    print(f"setup error: {msg}", file=sys.stderr)
    return 3


def _cmd_run(args) -> int:
    # Provider SDKs (OpenAI/Anthropic/LangSmith) read os.environ, not our Settings, so load the
    # git-ignored .env into the process without overriding anything already exported.
    from dotenv import load_dotenv
    load_dotenv(override=False)
    try:
        s = get_settings()
    except ValidationError as exc:
        return _setup_error(f"settings invalid ({exc.error_count()} errors); is DATABASE_URL set?")
    install_log_redaction()
    for secret in (s.tavily_api_key, s.database_url, s.admin_database_url):
        register_secret(secret.get_secret_value() if secret else None)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LANGSMITH_API_KEY"):
        register_secret(os.environ.get(name))
    mode = args.mode or SUITE_MODES[args.suite]
    if args.suite == "golden" and mode != "scripted":
        return _setup_error("the golden suite is scripted only")
    if s.admin_database_url is None:
        return _setup_error("ADMIN_DATABASE_URL is required (evals create throwaway tenants)")
    if mode in ("frozen", "live") and not s.llm_model:
        return _setup_error("LLM_MODEL is not set")
    if mode == "live" and s.tavily_api_key is None:
        return _setup_error("TAVILY_API_KEY is not set (live mode)")

    cases = load_suite(args.suite)
    if args.filter:
        cases = [c for c in cases if args.filter in c.id or c.id in {x.control for x in cases
                 if args.filter in x.id}]
    if not cases:
        if args.suite == "calibration":
            print("calibration: not run (no labels: add "
                  "evals/datasets/lead_research/calibration/labelled_companies.json)")
            return 0
        return _setup_error(f"no cases for suite {args.suite!r}")

    if args.langsmith and s.trace_redaction:
        from agents.guardrails.tracing import configure_trace_redaction
        configure_trace_redaction()

    repeats = args.repeats or s.eval_default_repeats
    target = LeadResearchTarget(s, timeout_s=s.eval_run_timeout_seconds)
    report = asyncio.run(run_suite(
        target, cases, suite=args.suite, mode=mode, default_repeats=repeats,
        concurrency=s.eval_concurrency, timeout_s=s.eval_run_timeout_seconds,
        max_runs=args.max_runs or s.eval_max_runs, git_sha=_git_sha(),
        price_llm=s.eval_price_per_llm_call,
        # frozen/scripted searches hit a local fixture: free. Only live mode pays for search.
        price_search=s.eval_price_per_search if mode == "live" else 0.0))

    jpath, _ = write_report(report, Path(args.out or s.eval_report_dir))
    print(render_markdown(report))
    print(f"report: {jpath}")
    if args.langsmith:
        from agents.evals.langsmith_sync import sync_report
        print(sync_report(report, cases))
    return _finish(report, args.update_baseline)


def _finish(report: SuiteReport, update_baseline: bool) -> int:
    gating_failed = [r.case_id for r in report.results if r.gating and not r.passed]
    code = 0
    if gating_failed or report.truncated:
        print(f"FAIL: gating cases failed: {gating_failed}; truncated={report.truncated}")
        code = 1
    base = regression.load_baseline(BASELINES, report.suite)
    if update_baseline:
        if code:
            print("baseline NOT updated: the run has failures")
        else:
            print(f"baseline written: {regression.save_baseline(report, BASELINES)}")
    elif base is None:
        print("no baseline yet (run with --update-baseline once the suite is accepted)")
    else:
        regs = regression.compare(report.metrics, base)
        if regs:
            print("REGRESSIONS vs baseline:\n  " + "\n  ".join(regs))
            code = code or 2
        else:
            print("no regressions vs baseline")
    return code


def _cmd_compare(args) -> int:
    report = SuiteReport.model_validate(json.loads(Path(args.report).read_text()))
    base = regression.load_baseline(BASELINES, report.suite)
    if base is None:
        print("no baseline for this suite")
        return 0
    regs = regression.compare(report.metrics, base)
    print("REGRESSIONS:\n  " + "\n  ".join(regs) if regs else "no regressions vs baseline")
    return 2 if regs else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m agents.evals")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a suite")
    r.add_argument("suite", choices=sorted(SUITE_MODES))
    r.add_argument("--mode", choices=["scripted", "frozen", "live"])
    r.add_argument("--repeats", type=int)
    r.add_argument("--max-runs", type=int)
    r.add_argument("--filter", help="only cases whose id contains this text (controls kept)")
    r.add_argument("--langsmith", action="store_true")
    r.add_argument("--update-baseline", action="store_true")
    r.add_argument("--out")
    c = sub.add_parser("compare", help="compare a saved report with the baseline")
    c.add_argument("report")
    args = p.parse_args(argv)
    return _cmd_run(args) if args.cmd == "run" else _cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
