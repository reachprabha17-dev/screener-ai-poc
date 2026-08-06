"""API request and response shapes (spec 15.4).

**Domain models for requests; explicit response models for reads.** That
asymmetry is deliberate. Requests are already validated by the domain contract,
so a parallel type hierarchy would be duplication. Responses need to control what
leaves the building.

The field that matters is `ScoredCriterion.model_verdict` — what the model said
*before* 10.5 forced it down. That is audit data. A reviewer's screen showing
both "none" and "the model originally said strong" invites exactly the
second-guessing the consistency gate exists to remove, and in an adverse-action
conversation it is a number nobody can defend. It is persisted, it is queryable
by an auditor, and it does not appear here.

The alternative — returning domain models directly and remembering to strip
fields — fails silently the first time someone adds a field to `Candidate`.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from screener.models import Band, Criterion, RedFlag, Verdict

# --- requests ----------------------------------------------------------------


class CreatePositionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    jd_text: str = Field(min_length=1)


class SaveRubricRequest(BaseModel):
    """A reviewer's edited rubric. Stored as a new version, never in place."""

    model_config = ConfigDict(extra="forbid")

    criteria: list[Criterion] = Field(min_length=4, max_length=12)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: str
    rubric_id: str


class OverrideRequest(BaseModel):
    """`reason` is required by the schema, not by convention.

    An override without one is unexplainable to the person it affected.
    """

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern="^(advance|reject|hold)$")
    reason: str = Field(min_length=1, max_length=2000)


# --- responses ---------------------------------------------------------------


class CriterionResponse(BaseModel):
    """No `model_verdict`. See the module docstring."""

    model_config = ConfigDict(extra="forbid")

    id: str
    verdict: Verdict
    evidence: str
    verified: bool
    # Surfaced so a reviewer can see *how well* the quote matched, not just
    # whether it passed. The thresholds are tunable and the numbers are what
    # 18.2 says to tune them against.
    match_ratio: float
    longest_span: int
    weight: int
    must_have: bool


class CandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str  # usually the person's name — this is a reviewer-facing screen
    file_sha256: str
    score: float | None  # None, never 0.0, when not scoreable
    band: Band | None
    must_haves_met: bool
    criteria: list[CriterionResponse]
    notable_strengths: list[str]
    red_flags: list[RedFlag]
    summary: str
    flags: list[str]
    scoreable: bool
    review_required: bool
    scored_at: datetime


class RankedResponse(BaseModel):
    """Three partitions, never one list.

    `needs_review` is its own collection so a UI cannot render it as the tail of
    a ranking, where at 1,000 applicants nobody would ever reach it (10.6).
    """

    model_config = ConfigDict(extra="forbid")

    meets_must_haves: list[CandidateResponse]
    missing_must_have: list[CandidateResponse]
    needs_review: list[CandidateResponse]
    escalation_rate: float


class PositionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    reference: str
    title: str
    created_by: str
    created_at: datetime


class RubricResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    position_id: str
    version: int
    criteria: list[Criterion]
    rubric_hash: str
    created_by: str
    approved_by: str | None
    approved_at: datetime | None

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    position_id: str
    rubric_id: str
    folder: str
    status: str
    model_digest: str
    prompt_hash: str
    app_version: str


class RunStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    total: int
    pending: int
    claimed: int
    done: int
    failed: int
    queue_depth_ahead: int
    eta_seconds: float
    escalation_rate: float


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    llm_reachable: bool
    model_digest_matches_pin: bool
    migrations_current: bool
    free_disk_gb: float
    disk_ok: bool
    app_version: str
    detail: dict[str, str]


class CountResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
