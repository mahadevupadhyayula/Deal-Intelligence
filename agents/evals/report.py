"""Metrics, Markdown/JSON rendering and safe report writing.

WHAT: `compute_metrics` (attack success rate, detection, agreement, calibration, stability,
cost), `render_markdown`, and `write_report` (redacted JSON + Markdown on disk).
WHERE: Used by the runner (metrics), the CLI (rendering/writing) and regression comparison.
WHY: One definition of each metric, so the baseline, the LangSmith experiment and the console
summary can never disagree. Attack success rate counts only runs that completed (an errored run
is reported separately as `error_rate`, and the case still fails, see `score_case`). Reports are
passed through `redact`/`redact_pii` before touching disk: outcomes contain quoted page text and
provider error strings, which is exactly where secrets and personal data would leak from.
"""
import json
from collections import Counter, defaultdict
from pathlib import Path

from agents.evals.models import CaseResult, EvalCase, SuiteReport
from agents.guardrails.redact import redact, redact_pii

DECISIVE_FIT = {"strong", "moderate"}
DECISIVE_NOT_FIT = {"weak", "not_fit"}


def _failed_repeats(res: CaseResult) -> set[int]:
    return {c.repeat for c in res.checks if not c.passed and c.repeat is not None}


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def compute_metrics(report: SuiteReport, cases: dict[str, EvalCase], price_llm: float = 0.0,
                    price_search: float = 0.0) -> tuple[dict, dict]:
    m: dict = {}
    gating_fail = defaultdict(int)
    gating_tot = defaultdict(int)
    nongating_fail = nongating_tot = detected = attack_runs = 0
    total_runs = errored = 0
    for res in report.results:
        total_runs += res.total_runs
        errored += res.errors
        if not res.attack_class:
            continue
        failed = _failed_repeats(res)
        for run in res.runs:
            if run.error is not None:
                continue
            attack_runs += 1
            detected += "injection_suspected" in run.outcome.get("flags", [])
            bad = run.repeat in failed
            if res.gating:
                gating_tot[res.attack_class] += 1
                gating_fail[res.attack_class] += bad
            else:
                nongating_tot += 1
                nongating_fail += bad
    g_tot, g_fail = sum(gating_tot.values()), sum(gating_fail.values())
    m["attack_success_rate"] = _rate(g_fail, g_tot)
    m["attack_success_by_class"] = {k: _rate(gating_fail[k], gating_tot[k]) for k in sorted(gating_tot)}
    m["attack_runs_gating"] = g_tot
    m["non_gating_attack_success_rate"] = _rate(nongating_fail, nongating_tot)
    m["detection_rate"] = _rate(detected, attack_runs)
    m["error_rate"] = _rate(errored, total_runs)

    judge = [r for r in report.results if "judgement" in cases[r.case_id].tags]
    m["judgement_agreement"] = _rate(sum(r.passed for r in judge), len(judge))
    gate = [r for r in report.results if r.gating]
    m["case_pass_rate"] = _rate(sum(r.passed for r in gate), len(gate))

    # --- calibration: labelled companies (tags "label:fit" / "label:not_fit") ---
    cal = [(cases[r.case_id], r) for r in report.results
           if any(t.startswith("label:") for t in cases[r.case_id].tags)]
    if cal:
        confusion: Counter = Counter()
        agree = decisive = insufficient = 0
        disagreements = []
        for case, res in cal:
            label = next(t.split(":", 1)[1] for t in case.tags if t.startswith("label:"))
            for run in res.runs:
                if run.error is not None:
                    continue
                tier = run.outcome.get("tier")
                confusion[f"{label}->{tier}"] += 1
                if tier == "insufficient_data":
                    insufficient += 1
                    continue
                decisive += 1
                ok = tier in (DECISIVE_FIT if label == "fit" else DECISIVE_NOT_FIT)
                agree += ok
                if not ok:
                    disagreements.append({"case": case.id, "label": label, "tier": tier,
                                          "score": run.outcome.get("score")})
        m["calibration"] = {"agreement": _rate(agree, decisive), "decisive_runs": decisive,
                            "insufficient_data_runs": insufficient,
                            "confusion": dict(sorted(confusion.items())),
                            "disagreements": disagreements}
        m["calibration_agreement"] = m["calibration"]["agreement"]

    # --- stability across repeats ---
    unstable, agreements = [], []
    for res in report.results:
        tiers = [r.outcome.get("tier") for r in res.runs if r.error is None]
        scores = [r.outcome.get("score") for r in res.runs if r.error is None
                  and r.outcome.get("score") is not None]
        if len(tiers) < 2:
            continue
        top = Counter(tiers).most_common(1)[0][1] / len(tiers)
        agreements.append(top)
        if top < 1.0:
            unstable.append({"case": res.case_id, "tiers": tiers,
                             "score_range": [min(scores), max(scores)] if scores else None})
    m["stability"] = {"mean_tier_agreement": round(sum(agreements) / len(agreements), 4)
                      if agreements else None, "unstable_cases": unstable}

    llm = sum(r.llm_calls for res in report.results for r in res.runs)
    srch = sum(r.searches for res in report.results for r in res.runs)
    dur = sum(r.duration_s for res in report.results for r in res.runs)
    cost = {"runs": total_runs, "llm_calls": llm, "searches": srch,
            "duration_s": round(dur, 1),
            "estimated_usd": round(llm * price_llm + srch * price_search, 4)}
    return m, cost


def render_markdown(report: SuiteReport) -> str:
    m = report.metrics
    lines = [f"# Eval report: {report.suite} ({report.mode})", "",
             f"- model: `{report.model}`  git: `{report.git_sha}`  started: {report.started_at:%Y-%m-%d %H:%M}Z",
             f"- cost estimate: {report.cost_estimate}", ""]
    if report.truncated:
        lines += [f"**TRUNCATED by --max-runs; skipped cases: {report.skipped}**", ""]
    lines += ["## Metrics", ""]
    for key in ("attack_success_rate", "non_gating_attack_success_rate", "detection_rate",
                "judgement_agreement", "case_pass_rate", "error_rate", "calibration_agreement"):
        if key in m:
            lines.append(f"- {key}: {m[key]}")
    if m.get("attack_success_by_class"):
        lines += ["", "| attack class | success rate |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in m["attack_success_by_class"].items()]
    if "calibration" in m:
        lines += ["", f"Calibration: {m['calibration']}"]
    lines += ["", f"Stability: {m['stability']}", "", "## Cases", "",
              "| case | class | gating | passed | runs | errors |", "|---|---|---|---|---|---|"]
    for r in report.results:
        lines.append(f"| {r.case_id} | {r.attack_class or ''} | {r.gating} | "
                     f"{'PASS' if r.passed else 'FAIL'} | {r.passed_runs}/{r.total_runs} | {r.errors} |")
    failing = [(r, c) for r in report.results for c in r.checks if not c.passed]
    if failing:
        lines += ["", "## Failing checks", ""]
        lines += [f"- `{r.case_id}` r{c.repeat} **{c.name}**: {c.detail}" for r, c in failing[:60]]
    return "\n".join(lines) + "\n"


def _scrub(text: str) -> str:
    return redact_pii(redact(text))


def write_report(report: SuiteReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{report.suite}-{report.mode}-{report.started_at:%Y%m%d-%H%M%S}"
    # Drop underscore-prefixed outcome keys (bulky private data, e.g. full source text).
    data = json.loads(report.model_dump_json())
    for res in data["results"]:
        for run in res["runs"]:
            run["outcome"] = {k: v for k, v in run["outcome"].items() if not k.startswith("_")}
    jpath, mpath = out_dir / f"{stamp}.json", out_dir / f"{stamp}.md"
    jpath.write_text(_scrub(json.dumps(data, indent=2, default=str)))
    mpath.write_text(_scrub(render_markdown(report)))
    return jpath, mpath
