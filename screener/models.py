"""Domain contracts (spec §5). Every value crossing a layer boundary is one of these.

This module imports nothing else from the package — it is the contract the API,
service, pipeline, core and storage layers all agree on, which is what lets any
one of them be replaced without touching the others.

Three shapes here carry decisions worth stating outright:

``JudgeOutput`` is the schema handed to the model and has no field for sentiment,
personality, or demographics. Schema omission alone is not sufficient, though —
``summary`` and ``notable_strengths`` are unconstrained strings and are where
that content actually lands, so they are screened separately (§10.7).

``RedFlag`` is a closed enum. Free-text red flags are a fairness hazard: models
reliably emit "employment gap" and "frequent job changes", which are proxies for
parental leave and disability.

``Candidate.score`` is ``None``, never ``0.0``, when a resume could not be judged.
A scanned PDF we failed to read is not a weak applicant, and the two must never
be indistinguishable in a ranking.
"""

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Verdict = Literal["strong", "partial", "none"]
Band = Literal["A", "B", "C", "D"]

VERDICT_VALUE: dict[Verdict, float] = {"strong": 1.0, "partial": 0.5, "none": 0.0}


def now() -> datetime:
    """The only permitted source of timestamps. Naive datetimes are a lint failure."""
    return datetime.now(UTC)


class Flag(StrEnum):
    """Conditions a reviewer must see.

    The split matters: deterministic flags describe a stable property of the
    input and may be cached, while transient flags describe an infrastructure
    hiccup. Caching a transient flag would serve a network blip back on resume
    as a permanent verdict (§12.5).
    """

    # deterministic — cacheable
    INPUT_REJECTED = "INPUT_REJECTED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    SUSPECTED_INJECTION = "SUSPECTED_INJECTION"
    SANITIZED_TEXT = "SANITIZED_TEXT"
    EVIDENCE_UNVERIFIED = "EVIDENCE_UNVERIFIED"
    EVIDENCE_CONTRADICTS = "EVIDENCE_CONTRADICTS"
    # The quote is real and verbatim, but is not about the criterion it was
    # offered for. Measured: `strong` on "Rust in production" evidenced by
    # "...migration ... to microservices in Go" — verifying at ratio 1.00.
    EVIDENCE_IRRELEVANT = "EVIDENCE_IRRELEVANT"
    VERDICT_SET_MISMATCH = "VERDICT_SET_MISMATCH"
    FREETEXT_SCREENED = "FREETEXT_SCREENED"
    MISSING_MUST_HAVE = "MISSING_MUST_HAVE"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    # transient — NEVER cached
    LLM_ERROR = "LLM_ERROR"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    PARSER_TIMEOUT = "PARSER_TIMEOUT"
    PARSER_CRASHED = "PARSER_CRASHED"


TRANSIENT_FLAGS: frozenset[Flag] = frozenset(
    {Flag.LLM_ERROR, Flag.SCHEMA_INVALID, Flag.PARSER_TIMEOUT, Flag.PARSER_CRASHED}
)


class RedFlag(StrEnum):
    """Closed set — see the module docstring for why free text is not allowed here."""

    CRITERION_CONTRADICTION = "CRITERION_CONTRADICTION"
    UNVERIFIABLE_CLAIM = "UNVERIFIABLE_CLAIM"
    INSTRUCTION_LIKE_TEXT = "INSTRUCTION_LIKE_TEXT"
    ILLEGIBLE_SECTION = "ILLEGIBLE_SECTION"


class Actor(BaseModel):
    """Threaded through every mutating call.

    Stubbed today (§15.2), real later. The plumbing is the expensive thing to
    retrofit, not the authentication.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    display_name: str = ""
    roles: frozenset[str] = frozenset()


# --- Position / Rubric ------------------------------------------------------


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    reference: str  # folder name under data/resumes/
    title: str
    jd_text: str  # provenance for the rubric derived from it
    created_by: str
    created_at: datetime


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str  # "C1".. assigned by Python, never by the model
    text: str
    must_have: bool = False
    weight: int = Field(default=1, ge=1, le=5)


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    position_id: str
    version: int
    # The cap lives in the contract, not "in the editor" — the extract path is an LLM.
    criteria: list[Criterion] = Field(min_length=4, max_length=12)
    created_by: str
    approved_by: str | None = None
    approved_at: datetime | None = None

    def by_id(self, criterion_id: str) -> Criterion | None:
        return next((c for c in self.criteria if c.id == criterion_id), None)

    @property
    def ids(self) -> set[str]:
        return {c.id for c in self.criteria}

    @property
    def total_weight(self) -> int:
        """Denominator for scoring — always over the rubric, never the returned set."""
        return sum(c.weight for c in self.criteria)

    @property
    def content_hash(self) -> str:
        """Identity of what this rubric *asks*, for the cache key (§6).

        Covers id, text, weight and must_have — everything that changes either
        the prompt or the arithmetic. Excludes the rubric's own id, version, and
        approval metadata: re-approving an unchanged rubric must not invalidate
        every judgment made under it.

        Order-sensitive, because criterion order is the order the model sees
        them in and reordering can change output.
        """
        payload = json.dumps(
            [[c.id, c.text, c.weight, c.must_have] for c in self.criteria],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- Intake -----------------------------------------------------------------


class ParsedResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    page_count: int
    ocr_used: bool
    chars_stripped: int = 0  # §8.6
    warnings: list[str] = Field(default_factory=list)
    parser_version: str


class ParseResult(BaseModel):
    """A resume, or the reason there isn't one. Never both, never an exception.

    Lives here rather than in ``intake/`` because it crosses a layer boundary:
    the parser produces it and the pipeline consumes it. A file that cannot be
    read is an expected, recordable state of a run — the candidate becomes
    unscoreable and a human sees them — so raising would put that decision in a
    ``try`` block instead of in the record.
    """

    model_config = ConfigDict(extra="forbid")

    parsed: ParsedResume | None = None
    flags: list[Flag] = Field(default_factory=list)
    error: str = ""
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.parsed is not None

    @property
    def security_event(self) -> bool:
        return Flag.PARSER_CRASHED in self.flags


# --- LLM output shapes ------------------------------------------------------
# These generate the JSON schemas passed to Ollama's constrained decoder, so the
# schema we ask for and the shape we parse cannot drift apart.


class CriterionVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    verdict: Verdict
    # The cap matters: §10.5's coverage ratio is meaningless against unbounded
    # evidence, because a long enough quote matches something in any resume.
    evidence: str = Field(max_length=300)


class ExtractedCriterion(BaseModel):
    """One criterion as the model proposes it — deliberately without an id.

    Ids are assigned by Python (``C1..Cn``) after extraction. Letting the model
    name them means the ids in the rubric and the ids it is asked to return at
    judging time come from the same unreliable source, and §10.3's set-equality
    check would be validating the model against itself.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    must_have: bool = False
    weight: int = Field(default=1, ge=1, le=5)


class ExtractedRubric(BaseModel):
    """Draft rubric from a job description. Always reviewed before use (§9.1)."""

    model_config = ConfigDict(extra="forbid")

    criteria: list[ExtractedCriterion] = Field(min_length=4, max_length=12)


class JudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criteria: list[CriterionVerdict]
    notable_strengths: list[str] = Field(default_factory=list, max_length=5)
    red_flags: list[RedFlag] = Field(default_factory=list)
    summary: str = Field(default="", max_length=600)


# --- Results ----------------------------------------------------------------


class ScoredCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    verdict: Verdict  # post-verification
    model_verdict: Verdict  # what the model said, retained for audit
    evidence: str
    verified: bool
    match_ratio: float  # persisted so thresholds are tuned against data (§18)
    longest_span: int
    weight: int
    must_have: bool


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    filename: str  # NOTE: usually contains the person's name
    file_sha256: str
    score: float | None = None
    band: Band | None = None
    must_haves_met: bool = False
    criteria: list[ScoredCriterion] = Field(default_factory=list)
    notable_strengths: list[str] = Field(default_factory=list)
    red_flags: list[RedFlag] = Field(default_factory=list)
    summary: str = ""
    flags: list[Flag] = Field(default_factory=list)
    scoreable: bool = True
    review_required: bool = False
    scored_at: datetime = Field(default_factory=now)

    @property
    def cacheable(self) -> bool:
        """False when any flag describes infrastructure rather than the input."""
        return not (set(self.flags) & TRANSIENT_FLAGS)


class Run(BaseModel):
    """A screening run and the inputs that determined its output.

    Every field below `folder` is a reproducibility record, not bookkeeping.
    Six months on, "why did this candidate score 6.2" is answerable only because
    the model digest, prompt hash and decoding parameters were captured at the
    time — the model tag will have moved and the prompt will have been edited,
    and neither leaves a trace anywhere else.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    position_id: str
    rubric_id: str
    folder: str
    status: Literal["pending", "running", "completed", "failed", "aborted"]
    model_digest: str
    prompt_hash: str
    redaction_on: bool
    num_ctx: int
    app_version: str
    escalation_rate: float | None = None
    reproducibility_rate: float | None = None


class RunStatus(BaseModel):
    """Live progress for `/runs/{id}/status` (§15.1).

    `escalation_rate` is surfaced here rather than computed at the end, because
    a reviewer discovering a 200-item review queue only after the run finishes
    is the §18.2 failure — by then the batch has already cost 78 minutes.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    total: int
    pending: int
    claimed: int
    done: int
    failed: int
    queue_depth_ahead: int = 0
    eta_seconds: float = 0.0
    escalation_rate: float = 0.0

    @property
    def is_complete(self) -> bool:
        return self.total > 0 and self.pending == 0 and self.claimed == 0


class HealthReport(BaseModel):
    """What `/health` and `/ready` report (§15.1, §17).

    Deliberately granular. "Unhealthy" alone tells an operator nothing at 2am,
    and the failure modes here have completely different responses: a stale model
    digest invalidates stored comparisons, a full disk stops the worker claiming,
    and pending migrations mean the process should not have started.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    llm_reachable: bool
    model_digest_matches_pin: bool
    migrations_current: bool
    free_disk_gb: float
    disk_ok: bool
    app_version: str
    detail: dict[str, str] = Field(default_factory=dict)


class RankedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meets_must_haves: list[Candidate] = Field(default_factory=list)
    missing_must_have: list[Candidate] = Field(default_factory=list)
    needs_review: list[Candidate] = Field(default_factory=list)
    # Surfaced, not buried: human oversight collapses into rubber-stamping once
    # the review queue exceeds what a person will actually read (§18.2).
    escalation_rate: float = 0.0
