"""Optional LangSmith mirror of an eval report.

WHAT: `sync_report` creates/updates a LangSmith dataset from the suite's cases and logs the
report as an experiment (one run per case repeat, scorer results as feedback).
WHERE: CLI `run --langsmith` (Gate 3). Never required.
WHY: The local JSON/Markdown report is the source of truth; LangSmith is a convenient viewer
and history. We log the runs we already made instead of calling `langsmith.evaluate`, which
would re-execute every case (double the model spend). Example ids are deterministic (uuid5) so
re-syncing a suite updates rather than duplicates. Any failure here is caught and reported:
losing the mirror must never fail an eval. The trace anonymiser is installed by the CLI first.
"""
import json
import uuid

from agents.evals.models import EvalCase, SuiteReport

_NS = uuid.UUID("6f1f7a3e-0b1a-4e0e-9a55-7d2f6f1d0c07")


def sync_report(report: SuiteReport, cases: list[EvalCase], client=None) -> str:
    """Returns a one-line status; never raises."""
    try:
        if client is None:
            from langsmith import Client

            client = Client()
        by_id = {c.id: c for c in cases}
        name = f"deal-intel-eval-{report.suite}"
        try:
            dataset = client.read_dataset(dataset_name=name)
        except Exception:  # noqa: BLE001  (any read failure => create the dataset)
            dataset = client.create_dataset(name, description=f"Spec 007 eval suite {report.suite}")
        example_ids = {}
        for c in cases:
            eid = uuid.uuid5(_NS, f"{report.suite}:{c.id}")
            example_ids[c.id] = eid
            try:
                client.create_example(
                    inputs=c.input, outputs={"checks": [k.model_dump() for k in c.checks]},
                    dataset_id=dataset.id, example_id=eid,
                    metadata={"attack_class": c.attack_class, "tags": c.tags})
            except Exception as exc:  # noqa: BLE001  (already exists from an earlier sync)
                _ = exc
        stamp = f"{report.started_at:%Y%m%d-%H%M%S}"
        project = f"eval-{report.suite}-{report.mode}-{report.git_sha or 'nogit'}-{stamp}"
        client.create_project(project_name=project, reference_dataset_id=dataset.id,
                              metadata={"model": report.model, "mode": report.mode,
                                        "metrics": json.loads(json.dumps(report.metrics))})
        for res in report.results:
            case = by_id.get(res.case_id)
            for run in res.runs:
                run_id = uuid.uuid4()
                client.create_run(
                    id=run_id, name=f"eval:{res.case_id}", run_type="chain",
                    inputs=case.input if case else {},
                    outputs={k: v for k, v in run.outcome.items() if not k.startswith("_")},
                    error=run.error, project_name=project,
                    reference_example_id=example_ids.get(res.case_id),
                    extra={"metadata": {"repeat": run.repeat}})
                for chk in (c for c in res.checks if c.repeat == run.repeat):
                    client.create_feedback(run_id, chk.name, score=1.0 if chk.passed else 0.0,
                                           comment=chk.detail[:500])
        return f"langsmith: logged experiment {project}"
    except Exception as exc:  # noqa: BLE001  (the mirror must never fail an eval)
        return f"langsmith: skipped ({type(exc).__name__}: {str(exc)[:120]})"
