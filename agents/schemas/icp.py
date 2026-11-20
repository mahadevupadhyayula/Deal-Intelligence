"""ICP segment definitions, criterion verdicts and score results.

WHAT: Pydantic v2 models for versioned ICP segments (weighted criteria, personas,
thresholds), the per-criterion verdicts an agent produces, and the resulting scores.
WHERE: Definitions are stored by `agents/repos/icp.py` and edited via `api/routes/icp.py`;
verdicts come from the Lead Research agent (Spec 005); `agents/icp/scoring.py` turns
verdicts + definitions into scores.
WHY: Criteria come in two kinds so each is judged by the right tool. `numeric` criteria
(e.g. employee count) are graded in CODE from an observed value, because arithmetic should
never depend on an LLM. `judged` criteria (company type, services...) carry a rubric the LLM
applies against evidence. Everything is `extra="forbid"` so LLM/user typos fail loudly.
"""
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from agents.schemas.base import TenantModel

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
Grade = Literal["ideal", "acceptable", "secondary", "none", "unknown"]
Tier = Literal["strong", "moderate", "weak", "not_fit", "insufficient_data"]
Rank = Literal["primary", "secondary"]

# Points per grade. `unknown` is deliberately absent: it is excluded from scoring rather
# than counted as zero, so missing evidence never looks like a bad fit.
GRADE_POINTS: dict[str, float] = {"ideal": 1.0, "acceptable": 0.75, "secondary": 0.5, "none": 0.0}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Band(_Strict):
    """A numeric range worth a grade. Bounds are inclusive; at least one is required."""

    grade: Literal["ideal", "acceptable", "secondary"]
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _bounds(self) -> "Band":
        if self.min is None and self.max is None:
            raise ValueError("a band needs at least one bound (min or max)")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("band min must be <= max")
        return self


class GradeHints(_Strict):
    ideal: str | None = None
    acceptable: str | None = None
    secondary: str | None = None


class _CriterionBase(_Strict):
    id: Slug
    label: str = Field(min_length=1)
    role: Literal["scoring", "disqualifier"] = "scoring"
    weight: int | None = Field(default=None, ge=1, le=10)

    @model_validator(mode="after")
    def _weight_rules(self) -> "_CriterionBase":
        if self.role == "scoring" and self.weight is None:
            raise ValueError(f"scoring criterion '{self.id}' needs a weight (1-10)")
        if self.role == "disqualifier" and self.weight is not None:
            raise ValueError(f"disqualifier '{self.id}' must not have a weight")
        return self


class NumericCriterion(_CriterionBase):
    """Graded in code from an observed number. Bands may overlap; the best grade wins
    (so 8-20 `ideal` inside 5-30 `acceptable` works as written)."""

    kind: Literal["numeric"]
    metric: str = Field(min_length=1)
    bands: list[Band] = Field(min_length=1)


class JudgedCriterion(_CriterionBase):
    """Judged by an LLM against cited evidence using this rubric."""

    kind: Literal["judged"]
    rubric: str = Field(min_length=1)
    grade_hints: GradeHints | None = None
    non_match_examples: list[str] = Field(default_factory=list)


Criterion = Annotated[NumericCriterion | JudgedCriterion, Field(discriminator="kind")]


class Persona(_Strict):
    """Who to target at a matching company. Stored, not scored (used by later specs)."""

    tier: Literal[1, 2]
    label: str = Field(min_length=1)
    roles: list[str] = Field(min_length=1)


class Thresholds(_Strict):
    strong: float = 75
    moderate: float = 50
    weak: float = 25

    @model_validator(mode="after")
    def _descending(self) -> "Thresholds":
        if not (100 >= self.strong > self.moderate > self.weak > 0):
            raise ValueError("thresholds must satisfy 100 >= strong > moderate > weak > 0")
        return self


class _SegmentBody(_Strict):
    """Fields shared by the API input and the stored/returned segment."""

    name: str = Field(min_length=1)
    rank: Rank
    description: str = ""
    criteria: list[Criterion] = Field(min_length=1, max_length=25)
    personas: list[Persona] = Field(default_factory=list)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    # Share of total scoring weight that must be known before we trust a tier.
    min_evidence_ratio: float = Field(default=0.5, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _criteria_rules(self) -> "_SegmentBody":
        ids = [c.id for c in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("criterion ids must be unique within a segment")
        if not any(c.role == "scoring" for c in self.criteria):
            raise ValueError("a segment needs at least one scoring criterion")
        return self


class IcpSegmentIn(_SegmentBody):
    """API input: the server assigns tenant, segment key (from the URL), version, timestamp."""


class IcpSegment(TenantModel, _SegmentBody):
    segment_key: Slug
    version: int = Field(ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IcpSegmentOut(_SegmentBody):
    """API output: a segment without the tenant id (callers already know their own)."""

    segment_key: Slug
    version: int
    created_at: datetime


class CriterionVerdict(TenantModel):
    """One judgement about one criterion. For numeric criteria only `observed_value` (plus
    evidence) matters: the grade is recomputed in code and any supplied grade is ignored."""

    criterion_id: Slug
    grade: Grade = "unknown"
    observed_value: str | float | None = None
    evidence_urls: list[AnyUrl] = Field(default_factory=list)
    rationale: str = ""

    @field_validator("observed_value", mode="before")
    @classmethod
    def _no_bool(cls, v):
        # Pydantic would silently coerce True -> 1.0, turning "True" into "1 employee".
        if isinstance(v, bool):
            raise ValueError("observed_value must be a number or string, not a boolean")  # noqa: TRY004 - pydantic needs ValueError
        return v


class SegmentScore(TenantModel):
    segment_key: Slug
    version: int
    rank: Rank
    score: float = Field(ge=0, le=100)
    tier: Tier
    evidence_ratio: float = Field(ge=0, le=1)
    contributions: dict[str, float] = Field(default_factory=dict)
    disqualified_by: list[str] = Field(default_factory=list)
    unverified_disqualifiers: list[str] = Field(default_factory=list)


class IcpResult(TenantModel):
    segments: list[SegmentScore]
    best_segment_key: str
    best_rank: Rank
    tier: Tier
    score: float = Field(ge=0, le=100)
