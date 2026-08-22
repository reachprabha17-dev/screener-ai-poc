"""Screening execution (spec 13). One resume in, one ``Candidate`` out.

This is the only place the eleven stages are composed, and **the order is the
specification**. Each constraint below exists because getting it wrong produces a
plausible-looking wrong answer rather than an error:

- `sanitize` runs before *everything*. Injection detection, redaction, budgeting
  and evidence matching all operate on text that has had invisible characters and
  bidi overrides removed — those tricks exist precisely to slip past checks like
  these, and they also silently break evidence matching (8.6).
- The budget check runs against the **assembled prompt**, not the resume. The
  system prompt, rubric and schema are the unmeasured slack that overflows.
- `verify_evidence` matches against `sent` — the exact sanitized, post-redaction
  string given to the model — never the original text. Matching anything else
  fails on every quote sitting near a redaction, and turns a working redactor
  into a source of false escalations.

**No failure path produces a score.** Every one returns a `Candidate` with
`score=None`, `scoreable=False`, `review_required=True` and a flag. Never `0.0`:
a resume we could not read must never be indistinguishable from a weak
candidate, because the ranking cannot tell them apart afterwards.

**This module does no I/O of its own beyond reading the file to hash it, and
touches no database.** It receives its collaborators through `Deps`, which is
what lets the whole pipeline be exercised against fakes without a GPU or a
sandbox (4 layering: pipeline → core, intake, clients, models — never storage).
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import settings
from screener.clients.ollama_client import BudgetBugError, LLMError, SchemaInvalidError
from screener.core.budget import check_budget
from screener.core.compute_score import compute_score
from screener.core.detect_injection import detect_injection
from screener.core.detect_negation import detect_negation
from screener.core.rank import assign_band
from screener.core.reconcile_judge import escalation_reasons_for, reconcile_judge
from screener.core.reconcile_relevance import reconcile_relevance
from screener.core.redact_pii import identity_map, redact_pii
from screener.core.screen_freetext import screen_freetext
from screener.core.verify_evidence import verify_evidence
from screener.intake.sanitize_text import sanitize
from screener.intake.validate_file import validate_file
from screener.llm import load_prompt
from screener.llm.confirm_absence import confirm_absence
from screener.llm.confirm_absence import targets as absence_targets
from screener.llm.confirm_relevance import confirm_relevance
from screener.llm.confirm_relevance import targets as relevance_targets
from screener.llm.judge_resume import PROMPT_NAME, build_user_message, judge_resume
from screener.llm.verify_support import targets as support_targets
from screener.llm.verify_support import verify_support
from screener.models import Candidate, Flag, Rubric, ScoredCriterion, Span, VerifyOutput, now
from screener.ports import LLMClient, ResumeParser

_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Deps:
    """Collaborators, injected. The seam that keeps this module testable."""

    parser: ResumeParser
    llm: LLMClient


@dataclass
class _Accumulator:
    """Flags and escalation state gathered as the stages run.

    Non-terminal findings (a sanitized document, a suspected injection) do not
    stop the pipeline but must survive to the final `Candidate` — losing them
    between stages is how a resume gets judged and quietly loses the one signal
    that said a human should look at it.
    """

    flags: list[Flag] = field(default_factory=list)
    review_required: bool = False

    def add(self, *flags: Flag, review: bool = False) -> None:
        for flag in flags:
            if flag not in self.flags:
                self.flags.append(flag)
        self.review_required = self.review_required or review


def file_sha256(path: Path) -> str:
    """Hash of the bytes actually read.

    Computed at screening time rather than at folder-scan time, so a file
    replaced between the snapshot and processing is identified as what was
    judged, not as what was queued (16.2). The cache key depends on this being
    the real bytes.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _unscoreable(
    *,
    run_id: str,
    path: Path,
    sha: str,
    acc: _Accumulator,
    summary: str = "",
    criteria: list[ScoredCriterion] | None = None,
    text: str = "",
    sent: str = "",
    span_map: list[Span] | None = None,
) -> Candidate:
    """The single shape every failure returns.

    `score=None`, never `0.0`. `review_required=True` unconditionally: something
    went wrong and a person has to see this candidate, whatever the cause.

    `criteria` is carried whenever the failure happened *after* judging. An
    escalated candidate is one a human now has to decide about, and they cannot
    do that from a flag alone — they need the verdicts, the quotes, and the
    `match_ratio` that explains why verification failed. Dropping them would make
    the review queue unactionable, which is the 18.2 failure where oversight
    collapses into rubber-stamping.

    `text`/`sent`/`span_map` are carried whenever parsing succeeded before the
    failure happened. A reviewer deciding an unscoreable candidate needs to read
    the document the same as any other — omitting it here just because scoring
    stopped is what silently blanked "Parsed resume text" for every escalated
    candidate who wasn't rejected at intake, which was never the intent (15.3).
    Callers earlier than parsing (8.2, 8.4) have nothing to pass and default to
    empty, which is correct: there is no text to show.
    """
    return Candidate(
        run_id=run_id,
        filename=path.name,
        file_sha256=sha,
        resume_text=text,
        sent_text=sent,
        redaction_map=span_map or [],
        score=None,
        band=None,
        must_haves_met=False,
        criteria=criteria or [],
        flags=acc.flags,
        summary=summary,
        scoreable=False,
        review_required=True,
        # Computed here too, not only on the happy path: an unprocessable
        # candidate is the one most likely to be lost at the bottom of a list,
        # and the review queue groups by reason (15.4). A candidate in the queue
        # with no reason is one nobody knows why they are looking at.
        escalation_reasons=escalation_reasons_for(acc.flags, criteria or []),
        scored_at=now(),
    )


def judge_one(  # noqa: PLR0911 — one return per terminal stage; collapsing them would hide the order
    path: Path,
    rubric: Rubric,
    deps: Deps,
    *,
    run_id: str,
    root: Path,
) -> Candidate:
    """Phase 1: judge one resume. Returns a `Candidate` in every case; never raises.

    Everything a candidate needs to be *ranked* happens here, and nothing that
    needs the verifier. The split is not organisational: 12 GB of VRAM cannot
    hold both models, so the two phases run as separate passes over the whole
    run and this function has to leave behind everything phase 2 will need —
    which is why `sent_text` and the span map are stored rather than recomputed
    (17.4, 12.6).

    `root` is the run folder, used to confirm the file has not escaped it. It is
    a parameter rather than a setting because it varies per run, and resolving it
    from configuration here would make containment depend on global state.
    """
    acc = _Accumulator()

    # --- 8.2 file validation, before anything opens the file properly -------
    check = validate_file(path, root=root)
    if not check.ok:
        acc.add(*check.flags, review=True)
        return _unscoreable(
            run_id=run_id,
            path=path,
            sha="",  # unreadable or untrusted: no hash is claimed
            acc=acc,
            summary=f"rejected at intake: {check.reason}",
        )

    sha = file_sha256(path)

    # --- 8.4 sandboxed parse ------------------------------------------------
    parse = deps.parser.parse(path)
    if not parse.ok or parse.parsed is None:
        acc.add(*parse.flags, review=True)
        return _unscoreable(run_id=run_id, path=path, sha=sha, acc=acc, summary=parse.error[:600])

    # --- 8.6 unicode sanitization, before every other text stage ------------
    text, stripped = sanitize(parse.parsed.text)
    if stripped:
        acc.add(Flag.SANITIZED_TEXT)
    if not text.strip():
        # Sanitization removed everything that was there. The document was
        # invisible characters and nothing else.
        acc.add(Flag.EXTRACTION_FAILED, review=True)
        return _unscoreable(run_id=run_id, path=path, sha=sha, acc=acc)

    # --- 10.2 injection detection: escalates, never excludes ---------------
    if settings.injection_detection:
        injection = detect_injection(text)
        if injection.detected:
            acc.add(Flag.SUSPECTED_INJECTION, review=True)

    # --- 10.x redaction. `sent` is what the model sees and what 10.5 matches
    #
    # `span_map` travels with them: evidence offsets address `sent`, HR reads
    # `text`, and the two only line up through the map (12.6). Deriving it later
    # from the two strings is not possible — a placeholder does not encode the
    # length of what it replaced.
    if settings.redact_pii:
        sent, _redaction, span_map = redact_pii(text)
    else:
        sent, span_map = text, identity_map(text)

    # --- 10.1 budget, against the assembled prompt --------------------------
    system = load_prompt(PROMPT_NAME)
    user = build_user_message(sent, rubric)
    budget = check_budget(deps.llm.count_prompt_tokens(settings.judge_model, system, user))
    if not budget.fits:
        # Never truncated and judged — that reintroduces at our own boundary the
        # silent overflow this check exists to prevent.
        acc.add(*budget.flags, review=True)
        return _unscoreable(
            run_id=run_id,
            path=path,
            sha=sha,
            acc=acc,
            summary=f"prompt {budget.prompt_tokens} tokens exceeds limit {budget.limit}",
            text=text,
            sent=sent,
            span_map=span_map,
        )

    # --- 11 + 10.3 judge, with one corrective retry on a verdict-set mismatch
    judged = judge_resume(deps.llm, sent, rubric)
    if judged.output is None or not judged.check.ok:
        acc.add(*judged.flags, review=True)
        return _unscoreable(
            run_id=run_id,
            path=path,
            sha=sha,
            acc=acc,
            summary=judged.check.describe(),
            text=text,
            sent=sent,
            span_map=span_map,
        )

    # --- 10.7 free-text screening, before anything is shown or scored -------
    screened = screen_freetext(judged.output)
    if screened.removed:
        acc.add(*screened.flags)

    # --- 10.5 evidence verification, against `sent` -------------------------
    verified = verify_evidence(screened.output, sent, rubric)

    # --- 10.5 C(2): a second, narrower opinion on what verify_evidence's crude
    # relevance check flagged. Same resident model, same phase — no reason for
    # a second phase when there is no second model (config/settings.py). Never
    # allowed to fail the candidate: an infrastructure hiccup here leaves the
    # flag exactly where verify_evidence left it, fail-safe rather than fatal.
    flagged_irrelevant = relevance_targets(verified.criteria)
    if flagged_irrelevant:
        try:
            checks = confirm_relevance(deps.llm, flagged_irrelevant, rubric, sent)
        except (LLMError, SchemaInvalidError, BudgetBugError):
            checks = []
        verified = reconcile_relevance(verified, checks)

    acc.add(*verified.flags, review=verified.review_required)

    if not verified.scoreable:
        # (b) could not confirm the quote. The verdict is left exactly as the
        # model returned it and the candidate leaves the ranking — escalation,
        # not a silent penalty on a heuristic this spec calls imperfect.
        # The criteria travel with them: this candidate is now a human's
        # decision, and a flag with no evidence is not a decision anyone can make.
        return _unscoreable(
            run_id=run_id,
            path=path,
            sha=sha,
            acc=acc,
            summary=screened.output.summary,
            criteria=verified.criteria,
            text=text,
            sent=sent,
            span_map=span_map,
        )

    # --- 10.5 C negation, over the blocks stage B just aligned ---------------
    #
    # After B because it reads the window before each verified block, and those
    # offsets only exist once the alignment has run. Flags only: a keyword window
    # is a heuristic, and the cost of being wrong in the downgrading direction is
    # an adverse outcome for a person produced by a word list.
    negation = detect_negation(verified.criteria, sent)
    acc.add(*negation.flags, review=negation.review_required)

    # --- 10.7 arithmetic ----------------------------------------------------
    scored = compute_score(negation.criteria, rubric)
    acc.add(*scored.flags, review=scored.review_required)

    return Candidate(
        run_id=run_id,
        filename=path.name,
        file_sha256=sha,
        resume_text=text,
        sent_text=sent,
        redaction_map=span_map,
        score=scored.score,
        band=assign_band(scored.score),
        must_haves_met=scored.must_haves_met,
        criteria=negation.criteria,
        notable_strengths=screened.output.notable_strengths,
        red_flags=screened.output.red_flags,
        summary=screened.output.summary,
        flags=acc.flags,
        scoreable=True,
        review_required=acc.review_required,
        escalation_reasons=escalation_reasons_for(acc.flags, negation.criteria),
        scored_at=now(),
    )


def verify_one(candidate: Candidate, rubric: Rubric, deps: Deps) -> Candidate:
    """Phase 2: a second model's opinion on what phase 1 concluded (10.6).

    Reads `sent_text` from the candidate rather than re-parsing the file — that
    is what makes this pass cheap, and it guarantees the verifier is looking at
    the exact string the judge was given rather than a re-derivation of it.

    **Two calls, both batched**, and both bounded by what they are allowed to
    change: nothing. `reconcile_judge` is pure and asserts that.

    An unscoreable candidate is skipped outright. Stage B already failed for
    them, they are already in the review queue, and 10.4 is explicit that D is
    skipped once B has failed — a second opinion on a quote we could not find is
    an invitation to confirm one that was never there.
    """
    if not candidate.scoreable:
        return candidate.model_copy(update={"verification_status": "skipped"})

    support = verify_support(
        deps.llm, support_targets(candidate.criteria), rubric, candidate.sent_text
    )
    absence = confirm_absence(
        deps.llm, absence_targets(candidate.criteria), rubric, candidate.sent_text
    )

    verified = reconcile_judge(
        candidate, VerifyOutput(support_checks=support, absence_checks=absence.checks)
    )

    if absence.skipped_over_budget:
        # The resume plus its `none` criteria did not fit `verifier_num_ctx`.
        # Flagged rather than truncated: Ollama truncates without an error, and a
        # confirmed absence from half a document is worse than no answer (10.1).
        flags = sorted({*verified.flags, Flag.BUDGET_EXCEEDED})
        return verified.model_copy(
            update={
                "flags": flags,
                "review_required": True,
                "escalation_reasons": escalation_reasons_for(flags, verified.criteria),
            }
        )
    return verified


def screen_batch(
    paths: list[Path],
    rubric: Rubric,
    deps: Deps,
    *,
    run_id: str,
    root: Path,
) -> list[Candidate]:
    """Screen a list of files serially. The break-glass path for `cli.py` (16).

    Serial, not concurrent: `OLLAMA_NUM_PARALLEL=1` means parallel calls would
    queue at Ollama anyway, and concurrency here would only add a way to lose
    results.

    Deliberately has **no queue, no resumption, and no persistence** — the worker
    owns those. This exists so a batch can be run with the API and database in an
    unknown state, which is exactly when the queue is not available to lean on.
    """
    return [judge_one(path, rubric, deps, run_id=run_id, root=root) for path in paths]
