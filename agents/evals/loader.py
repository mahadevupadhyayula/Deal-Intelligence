"""Dataset loader and validator.

WHAT: Loads a suite directory of case JSON files into `EvalCase` objects and validates the set.
WHERE: First step of every CLI run, and of the loader unit tests.
WHY: Datasets are code-reviewed data; bad ones should fail before any model call costs money.
Beyond schema validation it rejects duplicate ids, dangling `control` references, secret-shaped
strings and non-reserved domains (a dataset must not point test traffic at real hosts).
"""
import json
import re
from pathlib import Path

from agents.evals.models import EvalCase

# Reserved/test TLDs only: attack text must never name a real, reachable host.
_RESERVED = (".example", ".invalid", ".test", ".localhost")
_URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)", re.IGNORECASE)
_EMAIL_HOST = re.compile(r"[\w.+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_SECRET = re.compile(r"\b(sk-[A-Za-z0-9]{10,}|tvly-[A-Za-z0-9]{8,}|lsv2_[A-Za-z0-9_]{8,})")


class DatasetError(ValueError):
    pass


def _scan_text(case_id: str, blob: str) -> None:
    if _SECRET.search(blob):
        raise DatasetError(f"{case_id}: secret-shaped string in dataset")
    hosts = [*_URL_HOST.findall(blob), *_EMAIL_HOST.findall(blob)]
    for host in hosts:
        if not host.lower().rstrip(".").endswith(_RESERVED):
            raise DatasetError(f"{case_id}: non-reserved domain {host!r} (use .example/.invalid)")


def load_cases(suite_dir: Path, corpora_dir: Path | None = None,
               check_hosts: bool = True) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for path in sorted(suite_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path.name}: invalid JSON ({exc})") from exc
        for item in raw if isinstance(raw, list) else [raw]:
            case = EvalCase.model_validate(item)
            if case.source_corpus:
                if corpora_dir is None:
                    raise DatasetError(f"{case.id}: source_corpus needs a corpora directory")
                cpath = corpora_dir / f"{case.source_corpus}.json"
                if not cpath.exists():
                    raise DatasetError(f"{case.id}: unknown source_corpus {case.source_corpus!r}")
                case = case.model_copy(update={"corpus": json.loads(cpath.read_text())})
            if check_hosts:
                _scan_text(case.id, json.dumps(case.model_dump(mode="json")))
            cases.append(case)
    validate_set(cases)
    return cases


def validate_set(cases: list[EvalCase]) -> None:
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise DatasetError(f"duplicate case ids: {sorted(dupes)}")
    known = set(ids)
    for c in cases:
        if c.control is not None and c.control not in known:
            raise DatasetError(f"{c.id}: control {c.control!r} is not in the suite")
        if c.control == c.id:
            raise DatasetError(f"{c.id}: a case cannot be its own control")


def order_controls_first(cases: list[EvalCase]) -> list[EvalCase]:
    """Run baselines before the attacks that reference them, so a spend cap that truncates the
    run never leaves an attack case with no control to compare against."""
    return sorted(cases, key=lambda c: (c.control is not None, c.id))
