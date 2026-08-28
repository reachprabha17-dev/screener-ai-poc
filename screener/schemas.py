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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, field_validator

from screener.core.offsets import translate_block
from screener.core.resume_paths import is_safe_reference
from screener.core.verify_evidence import align
from screener.models import (
    Actor,
    Band,
    Candidate,
    Criterion,
    Decision,
    MatchBlock,
    RedFlag,
    ScoredCriterion,
    Span,
    Support,
    Verdict,
)

EvidenceStatus = Literal["verified", "partial", "unverified", "not_applicable"]

# --- requests ----------------------------------------------------------------


class CreatePositionRequest(BaseModel):
    """A requisition. `jd_text` is the description whatever produced it.

    The provenance fields are what the reviewer's browser echoes back from
    `POST /jd-documents`; they default to a pasted description, which is what a
    client that knows nothing about upload is sending. They are a record of
    origin, not a claim about `jd_text` — the reviewer edits the extracted text
    before submitting it, deliberately, so the hash names the uploaded document
    and nothing more.
    """

    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    jd_text: str = Field(min_length=1)
    jd_source: Literal["paste", "upload"] = "paste"
    jd_filename: str | None = Field(default=None, max_length=255)
    jd_file_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    jd_ocr_used: bool | None = None

    @field_validator("reference")
    @classmethod
    def validate_reference(cls, v: str) -> str:
        if not is_safe_reference(v):
            raise ValueError("must contain only letters, numbers, spaces, and & ' ( ) + # . _ -")
        return v


class JdDocumentResponse(BaseModel):
    """What `POST /jd-documents` returns: the text, and how to judge it.

    Everything beside `text` exists so the reviewer can tell whether the reading
    is trustworthy before a rubric is drafted from it. `ocr_used` in particular
    is not decoration — a scanned document has been *approximated*, and the
    person approving the rubric is the only one positioned to notice that the
    approximation dropped something.

    `injection_signals` and `warnings` are advisory. Nothing here blocks.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    filename: str
    file_sha256: str
    page_count: int
    ocr_used: bool
    chars_stripped: int
    warnings: list[str]
    injection_signals: list[str]
    parser_version: str


class ConfigResponse(BaseModel):
    """Deployment policy the interface has to agree with (7).

    Deliberately **not** part of `/health`. That endpoint is polled every thirty
    seconds and answers "can this process do work"; this one answers "what is
    this deployment configured to allow", which changes only on restart and is
    a different question with a different cache lifetime.

    Nothing derived from a secret, a path, or a host may be added here — it is
    served to every browser that can reach the port.
    """

    model_config = ConfigDict(extra="forbid")

    jd_intake_mode: Literal["both", "upload", "paste"]
    jd_max_file_bytes: int
    jd_max_pages: int
    allowed_extensions: list[str]


class FolderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    file_count: int
    has_subfolders: bool = False


class FolderPageResponse(BaseModel):
    """One page of the folder picker, plus the total it was drawn from.

    `total` is what lets the UI say "16–30 of 214" and disable the last page;
    without it a client cannot tell a full page from the final one.
    """

    model_config = ConfigDict(extra="forbid")

    folders: list[FolderResponse]
    total: int


class SaveRubricRequest(BaseModel):
    """A reviewer's edited rubric. Stored as a new version, never in place."""

    model_config = ConfigDict(extra="forbid")

    criteria: list[Criterion] = Field(min_length=4, max_length=12)
    # The version the editor was looking at (23.1.7). Optional so a first save
    # against a position with no rubric needs no ceremony; supplied by the UI,
    # which always knows what it loaded.
    base_version: int | None = None


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: str
    rubric_id: str


class DecisionRequest(BaseModel):
    """`reason` is required by the schema, not by convention.

    A decision without one is unexplainable to the person it affected.
    """

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern="^(advance|reject|hold)$")
    reason: str = Field(min_length=1, max_length=2000)


class BulkDecisionRequest(DecisionRequest):
    """One reason shared across many candidates.

    The shared reason is the reason bulk actions exclude `review_required`
    candidates: "does not meet the Python must-have" is a fair account of 300
    rejections and no account at all of the one the system could not read.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[int] = Field(min_length=1, max_length=1000)


class BulkDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decided: list[int]
    skipped: list[int]


# --- responses ---------------------------------------------------------------


class HighlightSpan(BaseModel):
    """A range to mark inside `resume_text` — already translated (15.3)."""

    model_config = ConfigDict(extra="forbid")

    start: int
    end: int


class VerifierView(BaseModel):
    """Present only when the second model disagreed (15.1).

    Absent on agreement, deliberately. A panel that always appears becomes
    furniture a reviewer scrolls past; one that appears only on disagreement is
    a signal.
    """

    model_config = ConfigDict(extra="forbid")

    disagrees: bool
    suggested_verdict: Verdict | None
    rationale: str
    found_evidence: str = ""
    found_highlights: list[HighlightSpan] = Field(default_factory=list)


class InjectionFindingView(BaseModel):
    """Which injection heuristic fired, and the text it fired on.

    Sent so the reviewer screen can show grounds instead of a bare warning. The
    excerpt is attacker-controlled resume text: the client must render it as
    text, never as markup.
    """

    signal: str
    excerpt: str


class CriterionView(BaseModel):
    """What a recruiter or hiring manager sees. No raw diagnostics (15.2)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    weight: int
    must_have: bool
    verdict: Verdict
    evidence: str
    evidence_status: EvidenceStatus
    highlights: list[HighlightSpan] = Field(default_factory=list)
    negation_suspected: bool = False
    verifier: VerifierView | None = None


class CriterionAuditView(CriterionView):
    """Everything above plus the numbers behind it. Auditors only (15.2)."""

    model_config = ConfigDict(extra="forbid")

    model_verdict: Verdict
    # Stage B: whether the judge's quote was actually found in the resume. A
    # verdict resting on a quote that could not be located is a different fact
    # from one that verified, and an audit that omits it cannot distinguish them.
    verified: bool = True
    match_ratio: float
    longest_span: int
    # Phase 2, in full. `verifier` above carries these too but only when the
    # second model *disagreed* — which leaves silence meaning either "it agreed"
    # or "it never ran". For an audit both halves have to be legible, so the
    # verifier's reading is carried here whatever it concluded.
    support: Support | None = None
    suggested_verdict: Verdict | None = None
    verifier_rationale: str = ""
    absence_confirmed: bool | None = None
    absence_evidence: str = ""


class CandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None
    filename: str  # usually the person's name — this is a reviewer-facing screen
    file_sha256: str
    score: float | None  # None, never 0.0, when not scoreable
    band: Band | None
    must_haves_met: bool
    resume_text: str
    criteria: list[CriterionView]
    notable_strengths: list[str]
    red_flags: list[RedFlag]
    summary: str
    flags: list[str]
    scoreable: bool
    review_required: bool
    escalation_reasons: list[str]
    # The grounds for SUSPECTED_INJECTION. Empty unless that flag is present —
    # a warning a reviewer cannot check is one they learn to clear unread.
    injection_findings: list[InjectionFindingView]
    # Surfaced so the UI can badge a candidate as provisional. A half-verified
    # result that renders identically to a finished one is how someone signs off
    # on work that has not happened yet (17.6).
    verification_status: str
    decision: Decision
    decided_by: str | None
    decided_at: datetime | None
    scored_at: datetime


class CandidateAuditResponse(CandidateResponse):
    """Adds the model's pre-verification view and the text it actually read.

    `sent_text` is here and nowhere else: it is the redacted string the model
    judged, and showing it to a recruiter alongside `resume_text` invites the
    question "which one is real?" about a document whose only job is to be read.
    """

    model_config = ConfigDict(extra="forbid")

    sent_text: str
    criteria: list[CriterionAuditView]  # type: ignore[assignment]


# --- domain → view ------------------------------------------------------------
#
# One mapper, chosen by role. The alternative — returning `Candidate` and
# trimming fields at each route — fails silently the first time someone adds a
# field to the domain model, and the failure is a disclosure rather than an
# error.

AUDITOR_ROLE = "auditor"


def evidence_status(criterion: ScoredCriterion) -> EvidenceStatus:
    """The badge that answers "can I trust this quote?" (15.1).

    Four states, and `partial` is the one worth having. A quote that mostly
    aligned but trails words the resume never contained is neither trustworthy
    nor junk — it is the case where the reviewer needs to see *which part*
    matched, which is what the highlights show.
    """
    if criterion.verdict == "none":
        # Nothing was quoted, so there is nothing to have verified. Rendering
        # this as `unverified` would put a warning badge on the system working
        # correctly.
        return "not_applicable"
    if not criterion.verified:
        return "unverified"
    return "verified" if criterion.match_ratio >= _FULL_MATCH else "partial"


# Above the stage-B floor but short of the whole quote. Not a tunable threshold
# for a decision — it only chooses a word on a badge, and the highlights beneath
# it carry the detail either way.
_FULL_MATCH = 0.95


def _highlights(blocks: list[MatchBlock], span_map: list[Span]) -> list[HighlightSpan]:
    """Translate `sent_text` offsets into `resume_text` offsets (15.3).

    Every highlight crosses this boundary. `match_blocks` address the redacted
    string the model read; HR reads the unredacted one, and redaction changes
    length at every substitution — so untranslated offsets do not land slightly
    off, they land progressively further off down the document.
    """
    translated = [translate_block(b, span_map) for b in blocks]
    return [HighlightSpan(start=b.doc_start, end=b.doc_end) for b in translated]


def _verifier_view(
    criterion: ScoredCriterion, candidate: Candidate, resume_text: str
) -> VerifierView | None:
    """Rendered only on disagreement — and never as a correction (15.5).

    `disagrees` is a fact about two models differing, which is why the field is
    named for the disagreement and not for a verdict. Presented as an answer,
    reviewers defer to it, and automated decision-making returns through the
    interface after being kept out of the code.
    """
    contradicted = criterion.support == "contradicted"
    found = criterion.absence_confirmed is False and bool(criterion.absence_evidence)
    if not (contradicted or found):
        return None

    highlights: list[HighlightSpan] = []
    if found:
        # The quote the verifier produced was re-verified against `sent_text` by
        # stage B before it was allowed to escalate; align it again here purely
        # to locate it for display.
        result = align(criterion.absence_evidence, candidate.sent_text)
        highlights = _highlights(result.blocks, candidate.redaction_map)

    return VerifierView(
        disagrees=True,
        suggested_verdict=criterion.suggested_verdict,
        rationale=criterion.verifier_rationale,
        found_evidence=criterion.absence_evidence,
        found_highlights=highlights,
    )


def criterion_view(criterion: ScoredCriterion, candidate: Candidate) -> CriterionView:
    return CriterionView(
        id=criterion.id,
        # Falls back to the id when the rubric no longer has this criterion. The
        # verdict is still shown — dropping it would hide what the model was
        # asked, which is the one thing an old result is evidence of.
        text=criterion.text or criterion.id,
        weight=criterion.weight,
        must_have=criterion.must_have,
        verdict=criterion.verdict,
        evidence=criterion.evidence,
        evidence_status=evidence_status(criterion),
        highlights=_highlights(criterion.match_blocks, candidate.redaction_map),
        negation_suspected=criterion.negation_suspected,
        verifier=_verifier_view(criterion, candidate, candidate.resume_text),
    )


def criterion_audit_view(criterion: ScoredCriterion, candidate: Candidate) -> CriterionAuditView:
    base = criterion_view(criterion, candidate)
    return CriterionAuditView(
        **base.model_dump(),
        model_verdict=criterion.model_verdict,
        verified=criterion.verified,
        match_ratio=criterion.match_ratio,
        longest_span=criterion.longest_span,
        support=criterion.support,
        suggested_verdict=criterion.suggested_verdict,
        verifier_rationale=criterion.verifier_rationale,
        absence_confirmed=criterion.absence_confirmed,
        absence_evidence=criterion.absence_evidence,
    )


def candidate_response(candidate: Candidate, actor: Actor) -> CandidateResponse:
    """The one place a `Candidate` becomes something a person may see.

    Role is read from the actor rather than passed as a flag, so a caller cannot
    request the auditor view by writing `audit=True` in a handler.
    """
    # Built field by field rather than from a dict of kwargs. A `**kwargs` splat
    # widens every value to a union and mypy stops checking the one construction
    # in the codebase where a wrong field is a disclosure rather than a bug.
    base = CandidateResponse(
        id=candidate.id,
        filename=candidate.filename,
        file_sha256=candidate.file_sha256,
        score=candidate.score,
        band=candidate.band,
        must_haves_met=candidate.must_haves_met,
        resume_text=candidate.resume_text,
        criteria=[criterion_view(c, candidate) for c in candidate.criteria],
        notable_strengths=candidate.notable_strengths,
        red_flags=candidate.red_flags,
        summary=candidate.summary,
        flags=[f.value for f in candidate.flags],
        scoreable=candidate.scoreable,
        review_required=candidate.review_required,
        escalation_reasons=[r.value for r in candidate.escalation_reasons],
        injection_findings=[
            InjectionFindingView(signal=f.signal, excerpt=f.excerpt)
            for f in candidate.injection_findings
        ],
        verification_status=candidate.verification_status,
        decision=candidate.decision,
        decided_by=candidate.decided_by,
        decided_at=candidate.decided_at,
        scored_at=candidate.scored_at,
    )
    if AUDITOR_ROLE not in actor.roles:
        return base

    return CandidateAuditResponse(
        **base.model_dump(exclude={"criteria"}),
        sent_text=candidate.sent_text,
        criteria=[criterion_audit_view(c, candidate) for c in candidate.criteria],
    )


class RankedResponse(BaseModel):
    """Three partitions, never one list.

    `needs_review` is its own collection so a UI cannot render it as the tail of
    a ranking, where at 1,000 applicants nobody would ever reach it (10.6).
    """

    model_config = ConfigDict(extra="forbid")

    # `SerializeAsAny`, because the auditor view is a *subclass* of the declared
    # type. Pydantic serializes a field to the type it is annotated with, so a
    # plain `list[CandidateResponse]` silently drops every audit field on the way
    # out — role scoping that reduces to "nobody gets anything", with no error.
    meets_must_haves: list[SerializeAsAny[CandidateResponse]]
    missing_must_have: list[SerializeAsAny[CandidateResponse]]
    needs_review: list[SerializeAsAny[CandidateResponse]]
    escalation_rate: float


class PositionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    reference: str
    title: str
    # Carried even though `GET /positions` lists open requisitions only: a
    # response that cannot express the state is one the client has to infer from
    # the absence of a row, and "not in the list" is also what a deleted
    # requisition looks like.
    status: str
    closed_at: datetime | None
    created_by: str
    created_at: datetime
    # Where the job description came from. Shown beside the rubric drafted from
    # it, because a rubric drafted from OCR'd text is drafted from an
    # approximation and the person approving it should be told so.
    jd_source: str
    jd_filename: str | None
    jd_ocr_used: bool | None


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
    judge_digest: str
    prompt_hash: str
    app_version: str
    created_at: datetime
    created_by: str
    file_count: int = 0
    escalation_rate: float | None = None


class FailedFileResponse(BaseModel):
    """A file that was never screened. Named, because a count is not actionable."""

    model_config = ConfigDict(extra="forbid")

    filename: str
    phase: str
    attempts: int
    last_error: str = ""


class RunStatusResponse(BaseModel):
    """Progress, plus which pass is producing it (15.4).

    `total` counts candidates — the judge phase — not jobs across both phases.
    The phase fields answer the different question a reviewer actually asks
    while a run is in flight: *which* 340 of 1000, judged or verified.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    total: int
    pending: int
    claimed: int
    done: int
    failed: int
    phase: str
    phase_done: int
    phase_total: int
    # Grouped, never a bare count. "23 need review" prompts a shrug; "8
    # unverified evidence, 7 judge disagreement" can be worked in batches.
    escalation_breakdown: dict[str, int]
    undecided_count: int
    queue_depth_ahead: int
    eta_seconds: float
    # Named files, not just the `failed` count above. Sign-off refuses while any
    # remain, so a reviewer needs to know which ones and why.
    failed_files: list[FailedFileResponse] = Field(default_factory=list)
    escalation_rate: float


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    llm_reachable: bool
    db_reachable: bool
    model_digest_matches_pin: bool
    migrations_current: bool
    free_disk_gb: float
    disk_ok: bool
    app_version: str
    detail: dict[str, str]


class CountResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int


class ReviewQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    position_reference: str
    position_title: str
    awaiting_review: int


class DashboardResponse(BaseModel):
    """Counts only.

    Nothing here is role-scoped, and that is a property to keep rather than an
    oversight: the moment a name, a filename or a reason appears on this
    response it becomes a read surface that needs the gate `GET /audit` has.
    Sizes of queues are not disclosures about the people in them.
    """

    model_config = ConfigDict(extra="forbid")

    open_positions: int
    applications: int
    awaiting_review: int
    runs_in_progress: int
    unscreened_files: int
    queues: list[ReviewQueueResponse] = Field(default_factory=list)


class AuditEntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: datetime
    actor_id: str | None
    action: str
    entity: str | None
    entity_id: str | None
    # Nullable because `detail_json` is: `append_for` is called without a detail
    # on actions whose entity id says everything (`sign_off_run`), and 15 of the
    # 90 rows in a working database have none. A non-optional dict here made the
    # page 500 on real data while passing every test, because no fixture wrote a
    # row without one.
    detail: dict[str, Any] | None = None


class DecisionRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: str
    from_decision: str
    to_decision: str
    old_score: float | None = None
    old_band: str | None = None
    reason: str
    at: datetime


class AdverseActionResponse(BaseModel):
    """Why one named person was rejected, and everything that determined it.

    Carries `CriterionAuditView` rather than `CriterionView`: this is the auditor
    surface, and `model_verdict` — what the judge said *before* the consistency
    gate forced it down — is exactly the number someone contesting an outcome is
    entitled to see. It is withheld from the reviewer screen for the opposite
    reason (15.4), and the role gate on the route is what keeps the two apart.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: int
    filename: str
    run_id: str
    position_reference: str
    decision: Decision
    decided_by: str | None = None
    decided_at: datetime | None = None
    history: list[DecisionRecordResponse] = Field(default_factory=list)
    score: float | None = None
    band: Band | None = None
    must_haves_met: bool = False
    scoreable: bool = True
    verification_status: str = "pending"
    criteria: list[CriterionAuditView] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    escalation_reasons: list[str] = Field(default_factory=list)
    injection_findings: list[InjectionFindingView] = Field(default_factory=list)
    summary: str = ""
    rubric_version: int | None = None
    rubric_hash: str = ""
    judge_digest: str = ""
    verifier_digest: str | None = None
    prompt_hash: str = ""
    app_version: str = ""
    redaction_on: bool = True
    scored_at: datetime | None = None
    rubric_approved_by: str | None = None
    run_signed_off_by: str | None = None


class RunStoryResponse(BaseModel):
    """One run's whole life, for the audit view.

    `separation_of_duties` is carried rather than re-derived: whether the person
    who approved the rubric is the person who accepted its results is a fact
    about the record, and a client recomputing it could disagree with the server.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    position_reference: str
    rubric_version: int | None = None
    approved_by: str | None = None
    signed_off_by: str | None = None
    events: list[AuditEntryResponse] = Field(default_factory=list)
    separation_of_duties: bool = False
    candidate_events_truncated: bool = False


class AuditPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[AuditEntryResponse]
    total: int
