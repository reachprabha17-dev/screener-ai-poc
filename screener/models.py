"""Domain contracts (spec 5). Every value crossing a layer boundary is one of these.

This module imports nothing else from the package — it is the contract the API,
service, pipeline, core and storage layers all agree on, which is what lets any
one of them be replaced without touching the others.

Three shapes here carry decisions worth stating outright:

``JudgeOutput`` is the schema handed to the model and has no field for sentiment,
personality, or demographics. Schema omission alone is not sufficient, though —
``summary`` and ``notable_strengths`` are unconstrained strings and are where
that content actually lands, so they are screened separately (10.7).

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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Verdict = Literal["strong", "partial", "none"]
Band = Literal["A", "B", "C", "D"]
# Phase 2 (10.6). `Support` is the verifier's reading of one quote against one
# claim; `Decision` is a human's disposal of a candidate. They are deliberately
# separate types — the verifier proposes and never disposes (1.9).
Support = Literal["supported", "insufficient", "contradicted"]
Decision = Literal["undecided", "advance", "hold", "reject"]

VERDICT_VALUE: dict[Verdict, float] = {"strong": 1.0, "partial": 0.5, "none": 0.0}


def now() -> datetime:
    """The only permitted source of timestamps. Naive datetimes are a lint failure."""
    return datetime.now(UTC)


class Flag(StrEnum):
    """Conditions a reviewer must see.

    The split matters: deterministic flags describe a stable property of the
    input and may be cached, while transient flags describe an infrastructure
    hiccup. Caching a transient flag would serve a network blip back on resume
    as a permanent verdict (12.5).
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
    NEGATION_SUSPECTED = "NEGATION_SUSPECTED"  # 10.5 C — the context inverts the quote
    JUDGE_DISAGREES = "JUDGE_DISAGREES"  # 10.6 A — second model reads it differently
    UNVERIFIED_ABSENCE = "UNVERIFIED_ABSENCE"  # 10.6 B — evidence found for a `none`
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


class EscalationReason(StrEnum):
    """Why a candidate is in the review queue (15.4).

    A count of 23 is not something a reviewer can act on; "8 unverified
    evidence, 7 judge disagreement, 4 absence found" is, because similar cases
    can then be worked as a batch. This is the difference between a queue that
    gets read and one that gets clicked through, and 19.2 says the queue being
    read is the whole oversight control.

    Separate from `Flag` on purpose: flags describe what happened to a criterion,
    reasons describe why a *person* needs to look. Several flags map to one
    reason, and some flags (SANITIZED_TEXT, POSSIBLE_DUPLICATE) map to none.
    """

    UNVERIFIED_EVIDENCE = "UNVERIFIED_EVIDENCE"
    JUDGE_DISAGREEMENT = "JUDGE_DISAGREEMENT"
    ABSENCE_FOUND = "ABSENCE_FOUND"
    NEGATION = "NEGATION"
    PARTIAL_MUST_HAVE = "PARTIAL_MUST_HAVE"
    UNPROCESSABLE = "UNPROCESSABLE"  # parse, budget or schema failure
    SUSPECTED_INJECTION = "SUSPECTED_INJECTION"


class RedFlag(StrEnum):
    """Closed set — see the module docstring for why free text is not allowed here."""

    CRITERION_CONTRADICTION = "CRITERION_CONTRADICTION"
    UNVERIFIABLE_CLAIM = "UNVERIFIABLE_CLAIM"
    INSTRUCTION_LIKE_TEXT = "INSTRUCTION_LIKE_TEXT"
    ILLEGIBLE_SECTION = "ILLEGIBLE_SECTION"


class InjectionFinding(BaseModel):
    """One injection heuristic that fired, and the text around where it fired.

    `SUSPECTED_INJECTION` on its own tells a reviewer that *something* matched
    and nothing about what, which is not a finding they can act on: the whole
    judgement is whether the matched text is an attack or is the candidate
    describing their job. `detect_injection` has produced both halves since it
    was written — its docstring says a reviewer "needs the whole picture" — and
    the pipeline discarded them at the point of setting the flag.

    `signal` is the pattern name (`role_hijack`, `template_marker`, …), and
    `excerpt` is the surrounding window, already whitespace-collapsed. The
    excerpt is attacker-controlled text: render it as data, never as markup, and
    never anywhere it could be read back as an instruction.
    """

    signal: str
    excerpt: str


class Actor(BaseModel):
    """Threaded through every mutating call.

    Stubbed today (15.2), real later. The plumbing is the expensive thing to
    retrofit, not the authentication.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    display_name: str = ""
    roles: frozenset[str] = frozenset()


class AuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: datetime
    actor_id: str | None
    action: str
    entity: str | None
    entity_id: str | None
    # Nullable because the column is. Several actions are audited with no detail
    # at all — the entity id carries the whole fact — and a required dict here
    # rejects them when they are read back.
    detail: dict[str, Any] | None = None


class RunStory(BaseModel):
    """One run's whole life, assembled from the audit log (15.4).

    The audit log answers "prove this hiring decision was made properly", and
    that question is always scoped to one thing rather than to the whole stream.
    A run is the useful scope: it reaches back through the rubric it was screened
    against to the requisition that raised it, and forward to the decisions and
    the sign-off.

    `separation_of_duties` is computed here rather than in a template. It is a
    statement about the record — whether the person who approved the rubric is
    the person who accepted its results — and a second implementation in the UI
    would be a second thing to keep true.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    position_reference: str
    rubric_version: int | None = None
    approved_by: str | None = None
    signed_off_by: str | None = None
    events: list[AuditEntry] = Field(default_factory=list)
    # False when one person did both, or when either half has not happened yet.
    # Not a rule violation — 14 permits it — but it is the first thing an auditor
    # looks for, so the record states it rather than leaving it to be noticed.
    separation_of_duties: bool = False
    # Candidate events are gathered per candidate; a large run is truncated
    # rather than issuing a query per applicant. The UI says so when it fires.
    candidate_events_truncated: bool = False


# --- Position / Rubric ------------------------------------------------------


class FolderInfo(BaseModel):
    """One folder on the resume share, as the picker sees it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    # Relative to `settings.resumes_dir`, `/`-separated. This is what becomes a
    # position's `reference`, so the picker never handles an absolute path.
    path: str
    file_count: int
    # Whether descending is worth offering. Cheaper than making the UI probe.
    has_subfolders: bool = False


JdSource = Literal["paste", "upload"]


class JdExtraction(BaseModel):
    """A job description read out of an uploaded document (spec 8, 9.1).

    Not persisted. It is the answer to one request — the text plus everything a
    reviewer needs to judge whether that text is a fair reading of the file they
    uploaded — and it becomes a `Position` only if they submit it.

    **`text` is post-sanitize.** The reviewer is shown the exact string that will
    reach the model, because showing them the raw extract and sanitizing
    afterwards would rebuild the reader/extractor divergence that 8.6 exists to
    close, inside the control meant to close it.

    `warnings` and `injection_signals` are advisory and never block. The
    correction step is the control here: the reviewer reads and edits the text
    before it is submitted, which is stronger than any heuristic. These only say
    where to look.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    filename: str
    # Of the uploaded *file*, not of `text` — the reviewer may edit the text
    # before submitting, deliberately. This identifies the document, and makes
    # no claim that the stored description is a faithful function of it.
    file_sha256: str
    page_count: int
    ocr_used: bool
    chars_stripped: int = 0  # 8.6
    warnings: list[str] = Field(default_factory=list)
    injection_signals: list[str] = Field(default_factory=list)
    parser_version: str


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    reference: str  # folder name under settings.resumes_dir
    title: str
    jd_text: str  # provenance for the rubric derived from it
    # How `jd_text` got here. A rubric drafted from OCR'd text is drafted from
    # an approximation, and a reviewer looking at an adverse outcome months
    # later has to be able to see that. `jd_filename`, `jd_file_sha256` and
    # `jd_ocr_used` are None for a pasted description — not applicable, rather
    # than unknown.
    jd_source: JdSource = "paste"
    jd_filename: str | None = None
    jd_file_sha256: str | None = None
    jd_ocr_used: bool | None = None
    # The requisition lifecycle, in the schema since 0001 and unread until now.
    # Closing takes a filled post off the working list; it deletes nothing, and
    # the runs it produced stay readable — the record of an adverse decision
    # cannot depend on whether somebody later tidied up.
    status: Literal["open", "closed"] = "open"
    closed_at: datetime | None = None
    created_by: str
    created_at: datetime


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str  # "C1".. assigned by Python, never by the model
    text: str
    # The criterion restated as an assertion about the candidate, consumed
    # directly by the phase-2 support check (10.6 A): "5+ years backend
    # engineering" becomes "The candidate has at least 5 years of backend
    # engineering experience." Produced once at extraction so the verifier is
    # not re-deriving a hypothesis per resume — a vague claim verifies vaguely.
    #
    # Defaulted rather than required, unlike the spec's literal signature,
    # because rubrics stored before v6 have no claim and must stay readable.
    # The gate is at approval (14), not at parse: an empty or stale claim blocks
    # approval, which is the last point before it can affect a candidate.
    claim: str = ""
    # Set when `text` is edited without regenerating `claim`. Without it the
    # verifier checks against a hypothesis that no longer matches the criterion,
    # and nothing about the output looks wrong.
    claim_stale: bool = False
    must_have: bool = False
    weight: int = Field(default=1, ge=1, le=5)

    @property
    def claim_usable(self) -> bool:
        return bool(self.claim.strip()) and not self.claim_stale


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
        """Identity of what this rubric *asks*, for the cache key (6).

        Covers id, text, weight, must_have and `claim` — everything that changes
        either a prompt or the arithmetic. Excludes the rubric's own id, version,
        and approval metadata: re-approving an unchanged rubric must not
        invalidate every judgment made under it.

        `claim` is in the hash because 10.6 feeds it to the verifier verbatim.
        Regenerating a claim without touching the criterion text changes what
        phase 2 checks, and verification is now part of the stored result — left
        out, an improved claim would be served the previous run's verification
        from cache. `claim_stale` is excluded: it is workflow state about whether
        a human still has work to do, not a description of what the rubric asks.

        Order-sensitive, because criterion order is the order the model sees
        them in and reordering can change output.
        """
        payload = json.dumps(
            [[c.id, c.text, c.claim, c.weight, c.must_have] for c in self.criteria],
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
    chars_stripped: int = 0  # 8.6
    warnings: list[str] = Field(default_factory=list)
    parser_version: str


class Span(BaseModel):
    """One *preserved* segment of text, carried in both coordinate systems (12.6).

    Redaction changes string length at every substitution, so an offset into
    `sent_text` (what the model saw) does not address the same characters in
    `resume_text` (what HR reads). A span map is one of these per surviving
    segment, built while walking the matches — it cannot be reconstructed
    afterwards from the two strings, because the placeholder text is not a
    function of what it replaced.
    """

    model_config = ConfigDict(extra="forbid")

    src_start: int  # into resume_text
    src_end: int
    dst_start: int  # into sent_text
    dst_end: int


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
    # The cap matters: 10.5's coverage ratio is meaningless against unbounded
    # evidence, because a long enough quote matches something in any resume.
    evidence: str = Field(max_length=300)


class ExtractedCriterion(BaseModel):
    """One criterion as the model proposes it — deliberately without an id.

    Ids are assigned by Python (``C1..Cn``) after extraction. Letting the model
    name them means the ids in the rubric and the ids it is asked to return at
    judging time come from the same unreliable source, and 10.3's set-equality
    check would be validating the model against itself.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    # The criterion restated as an assertion the phase-2 support check can test
    # (9.1, 10.6 A). Written here, once per rubric, rather than derived per
    # resume: a hypothesis re-invented 1,000 times is 1,000 chances to drift, and
    # the verifier's answer is only as sharp as the question.
    claim: str = ""
    must_have: bool = False
    weight: int = Field(default=1, ge=1, le=5)


class ExtractedRubric(BaseModel):
    """Draft rubric from a job description. Always reviewed before use (9.1)."""

    model_config = ConfigDict(extra="forbid")

    criteria: list[ExtractedCriterion] = Field(min_length=4, max_length=12)


class JudgeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criteria: list[CriterionVerdict]
    notable_strengths: list[str] = Field(default_factory=list, max_length=5)
    red_flags: list[RedFlag] = Field(default_factory=list)
    summary: str = Field(default="", max_length=600)


# --- Phase 2: the verifier (10.6) -------------------------------------------


class SupportCheck(BaseModel):
    """Does the quoted excerpt actually establish the claim?"""

    model_config = ConfigDict(extra="forbid")

    id: str
    support: Support
    # Always stated, even on agreement. A verdict the excerpt *would* justify is
    # something a reviewer can act on; "I disagree" without an alternative is
    # not, and asking for it unconditionally keeps the field's presence from
    # being a signal in itself.
    suggested_verdict: Verdict
    rationale: str = Field(default="", max_length=200)


class AbsenceCheck(BaseModel):
    """Is this criterion genuinely absent, or did the judge miss it?

    `found_evidence` must be a verbatim quote: it is re-verified through stage B
    before it is allowed to escalate anything (10.6 B). Without that, one
    unverifiable assertion would override another.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    confirmed_absent: bool
    found_evidence: str = Field(default="", max_length=300)


class VerifyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    support_checks: list[SupportCheck] = Field(default_factory=list)
    absence_checks: list[AbsenceCheck] = Field(default_factory=list)


class RelevanceCheck(BaseModel):
    """Is this excerpt about the same subject as the criterion, just in different words?

    Only ever asked about a criterion `verify_evidence`'s crude keyword-overlap
    check already flagged `EVIDENCE_IRRELEVANT` — a plain topic question, not a
    depth or sufficiency one (`SupportCheck` covers that, separately, when
    enabled). `related=True` is the only answer that clears the flag; anything
    else — disagreement, or no answer at all — leaves it exactly where it was.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    related: bool
    rationale: str = Field(default="", max_length=200)


class RelevanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks: list[RelevanceCheck] = Field(default_factory=list)


# --- Results ----------------------------------------------------------------


class MatchBlock(BaseModel):
    """One aligned run of tokens, as **character** offsets (10.5 B, 12.6).

    Characters rather than token indices so the UI renders directly without
    re-tokenizing — a second tokenizer in the read path is a second thing to
    keep in step. `doc_*` address `sent_text`; the read layer translates them
    into `resume_text` coordinates through the span map.

    This is what makes a `match_ratio` of 0.42 interrogable. The reviewer's
    question is never "what was the ratio", it is "which part of the quote was
    not in the resume", and only the blocks answer it.
    """

    model_config = ConfigDict(extra="forbid")

    ev_start: int  # into the evidence string
    ev_end: int
    doc_start: int  # into sent_text
    doc_end: int


class ScoredCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    verdict: Verdict  # post-verification
    model_verdict: Verdict  # what the model said, retained for audit
    evidence: str
    verified: bool
    match_ratio: float  # persisted so thresholds are tuned against data (18)
    longest_span: int
    match_blocks: list[MatchBlock] = Field(default_factory=list)
    negation_suspected: bool = False
    # Set by verify_evidence's crude keyword-overlap relevance check
    # (Flag.EVIDENCE_IRRELEVANT). Per-criterion, unlike the flag itself, so a
    # follow-up semantic check (confirm_relevance) knows exactly which
    # criteria to ask about rather than re-deriving the same crude check.
    evidence_irrelevant: bool = False
    # Phase 2 — null until verification has run, which is distinguishable from
    # "ran and agreed" (`support == "supported"`). A reviewer signing off needs
    # to be able to tell those apart (17.6).
    support: Support | None = None
    suggested_verdict: Verdict | None = None
    verifier_rationale: str = ""
    absence_confirmed: bool | None = None
    absence_evidence: str = ""
    # Rubric facts, re-attached on read rather than stored per verdict — a second
    # copy would be a second source of truth for the scoring arithmetic and for
    # what the criterion actually said. `text` defaults empty because it is only
    # needed by the read layer; nothing in scoring reads it.
    text: str = ""
    weight: int
    must_have: bool


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None  # assigned by the store; None until first saved
    run_id: str
    filename: str  # NOTE: usually contains the person's name
    file_sha256: str
    # Three text versions exist, two are persisted (12.6). `resume_text` is
    # sanitized with names intact and is the only one ever rendered to HR;
    # `sent_text` is that plus redaction — the exact string the model judged,
    # and the string every offset in `match_blocks` addresses. Raw pre-sanitize
    # text is never stored: bidi and zero-width characters render in HTML
    # exactly as designed, so showing it would undo 8.6 at the last step.
    resume_text: str = ""
    sent_text: str = ""
    redaction_map: list[Span] = Field(default_factory=list)
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
    escalation_reasons: list[EscalationReason] = Field(default_factory=list)
    # Why `SUSPECTED_INJECTION` fired. Empty whenever it did not.
    injection_findings: list[InjectionFinding] = Field(default_factory=list)
    # `pending` is not a synonym for `skipped`: a candidate whose verification
    # has not run yet is displayed as provisional and counts as review-required
    # at sign-off, because partial verification must never look like completed
    # verification (17.6).
    verification_status: Literal["pending", "done", "skipped"] = "pending"
    # Decision state: current value here, history in `overrides` (12.8).
    # `undecided` as the default is what distinguishes a run nobody reviewed
    # from one that was reviewed and left alone.
    decision: Decision = "undecided"
    decided_by: str | None = None
    decided_at: datetime | None = None
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
    # `empty` is not a flavour of `completed`. A run over a folder with no
    # eligible files must say so — a reviewer shown a blank results screen with
    # a green tick has no way to tell "nobody applied" from "something broke"
    # (17.2).
    status: Literal["pending", "running", "completed", "empty", "failed", "aborted"]
    # Two-phase execution (17.4). `judge` and `verify` are work; `done` means
    # both passes are finished and is what stops a completed run being re-entered.
    phase: Literal["judge", "verify", "done"] = "judge"
    judge_digest: str
    verifier_model: str | None = None
    verifier_digest: str | None = None
    verification_enabled: bool = True
    prompt_hash: str
    redaction_on: bool
    num_ctx: int
    app_version: str
    file_count: int = 0
    escalation_rate: float | None = None
    created_at: datetime = Field(default_factory=now)
    created_by: str
    # The named human who accepted the results. Half of the separation-of-duties
    # pair — the other half is `rubrics.approved_by` — and the reason sign-off is
    # an artefact rather than a status change.
    reviewed_by: str | None = None
    reproducibility_rate: float | None = None


class DecisionRecord(BaseModel):
    """One decision, and the grounds given for it."""

    model_config = ConfigDict(extra="forbid")

    actor_id: str
    from_decision: str
    to_decision: str
    old_score: float | None = None
    old_band: str | None = None
    reason: str
    at: datetime


class AdverseActionRecord(BaseModel):
    """Why one named person was rejected, and everything that determined it.

    **The artefact handed to a regulator, an ombudsman, or the applicant.** The
    question is never "what did the system score them" alone — it is "on what
    criteria, against which approved rubric, judged by which model weights, and
    who decided, on what stated grounds". Every one of those is already frozen at
    scoring time on the candidate row; this assembles them into one answer rather
    than leaving them to be joined by hand across four tables.

    Assembled for any decision, not only rejections. A record that only exists
    for adverse outcomes is a record nobody can check against a favourable one.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: int
    filename: str
    run_id: str
    position_reference: str
    decision: Decision
    decided_by: str | None = None
    decided_at: datetime | None = None
    # The grounds. Read from `overrides`, which is the only place a reason is
    # stored — `candidates` keeps the standing decision, never why it was taken.
    history: list[DecisionRecord] = Field(default_factory=list)
    score: float | None = None
    band: Band | None = None
    must_haves_met: bool = False
    scoreable: bool = True
    # Whether phase 2 ran at all. Without it, a record showing no verifier
    # disagreement is ambiguous between "the second model agreed", "it was
    # skipped" and "it has not run yet" — three different things to an auditor.
    verification_status: Literal["pending", "done", "skipped"] = "pending"
    # Per-criterion, with the rubric text rejoined so a criterion id means
    # something to a reader who has never seen the rubric.
    criteria: list[ScoredCriterion] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    escalation_reasons: list[EscalationReason] = Field(default_factory=list)
    # The grounds for `SUSPECTED_INJECTION`, for the same reason every other
    # field here exists: a flag with no stated basis is not answerable later.
    injection_findings: list[InjectionFinding] = Field(default_factory=list)
    summary: str = ""
    # The reproducibility record, copied onto the candidate when it was scored.
    # Months later these are what make the outcome re-derivable; the model tag
    # will have moved and the prompt will have been edited.
    rubric_version: int | None = None
    rubric_hash: str = ""
    judge_digest: str = ""
    verifier_digest: str | None = None
    prompt_hash: str = ""
    app_version: str = ""
    redaction_on: bool = True
    scored_at: datetime | None = None
    # Who approved the rubric this person was judged against, and who accepted
    # the run's results. The two human gates, named.
    rubric_approved_by: str | None = None
    run_signed_off_by: str | None = None


class FailedFile(BaseModel):
    """A file that was never screened, and why.

    Distinct from a flagged `Candidate`: a document the parser rejected still
    becomes a candidate a human sees. This is a file the system failed to process
    for reasons that say nothing about the applicant — and which, until it is
    surfaced, exists in no results table at all.
    """

    model_config = ConfigDict(extra="forbid")

    filename: str
    phase: str
    attempts: int
    last_error: str = ""


class RunStatus(BaseModel):
    """Live progress for `/runs/{id}/status` (15.1).

    `escalation_rate` is surfaced here rather than computed at the end, because
    a reviewer discovering a 200-item review queue only after the run finishes
    is the 18.2 failure — by then the batch has already cost 78 minutes.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    # `total` and the counts below are the **judge** phase: one job per
    # candidate, which is what everyone means by the size of a run. Counting
    # both phases would report a 6-file run as 12 the moment verification was
    # switched on.
    total: int
    pending: int
    claimed: int
    done: int
    failed: int
    # Which pass is running, and how far through it. "judging 340/1000" and
    # "verifying 120/1000" are different facts and a reviewer needs to know
    # which one they are looking at (15.4).
    phase: Literal["judge", "verify", "done"] = "judge"
    phase_done: int = 0
    phase_total: int = 0
    escalation_breakdown: dict[EscalationReason, int] = Field(default_factory=dict)
    undecided_count: int = 0
    queue_depth_ahead: int = 0
    eta_seconds: float = 0.0
    # Named, not just counted. `failed` above is a number a reviewer can do
    # nothing with; these are the files behind it, and sign-off refuses while
    # any remain.
    failed_files: list[FailedFile] = Field(default_factory=list)
    escalation_rate: float = 0.0

    @property
    def is_complete(self) -> bool:
        return self.total > 0 and self.pending == 0 and self.claimed == 0


class ReviewQueue(BaseModel):
    """One run's outstanding review queue, named so it can be worked.

    A total on its own is not actionable: review happens inside a run, so
    "31 candidates to review" is only a number until it says *which* runs hold
    them. This is the same reasoning that grouped escalations by reason rather
    than reporting one count (15.4).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    position_reference: str
    position_title: str
    awaiting_review: int


class DashboardSummary(BaseModel):
    """The three numbers a manager opens the system to see, and how to act on them.

    **`awaiting_review` uses the sign-off predicate, not a looser one.** A
    candidate counts when it is undecided *and* either escalated or still
    unverified — exactly the condition `sign_off_run` refuses on. Any other
    definition produces a dashboard that reads zero while the sign-off button
    returns 400, which teaches reviewers to distrust both.

    **`applications` counts a CV once per requisition it was sent to**, however
    many runs screened it. Re-running a folder after fixing a parser failure
    creates a second candidate row for the same file; counting rows would report
    that as new applicants arriving.
    """

    model_config = ConfigDict(extra="forbid")

    open_positions: int = 0
    applications: int = 0
    awaiting_review: int = 0
    # Context for the other three rather than headline numbers: applications
    # rises while these are non-zero, and a queue that is not moving is a
    # different problem from one that has not been screened yet.
    runs_in_progress: int = 0
    unscreened_files: int = 0
    queues: list[ReviewQueue] = Field(default_factory=list)


class HealthReport(BaseModel):
    """What `/health` and `/ready` report (15.1, 17).

    Deliberately granular. "Unhealthy" alone tells an operator nothing at 2am,
    and the failure modes here have completely different responses: a stale model
    digest invalidates stored comparisons, a full disk stops the worker claiming,
    and pending migrations mean the process should not have started.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    llm_reachable: bool
    # Separate from `migrations_current`: a database that cannot be reached is a
    # different incident from one whose schema is behind, and reporting the first
    # as the second sends an operator to look at migrations at 2am.
    db_reachable: bool = True
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
    # the review queue exceeds what a person will actually read (18.2).
    escalation_rate: float = 0.0
    # The breakdown, not just the rate. Grouping is what makes a queue of 23
    # workable — see EscalationReason.
    escalation_breakdown: dict[EscalationReason, int] = Field(default_factory=dict)
    undecided_count: int = 0
