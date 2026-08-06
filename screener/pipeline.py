"""Screening execution (spec §13). One resume in, one ``Candidate`` out.

This is the only place the eleven stages are composed, and **the order is the
specification**. Each constraint below exists because getting it wrong produces a
plausible-looking wrong answer rather than an error:

- `sanitize` runs before *everything*. Injection detection, redaction, budgeting
  and evidence matching all operate on text that has had invisible characters and
  bidi overrides removed — those tricks exist precisely to slip past checks like
  these, and they also silently break evidence matching (§8.6).
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
sandbox (§4 layering: pipeline → core, intake, clients, models — never storage).
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import settings
from screener.core.budget import check_budget
from screener.core.compute_score import compute_score
from screener.core.detect_injection import detect_injection
from screener.core.rank import assign_band
from screener.core.redact_pii import redact_pii
from screener.core.screen_freetext import screen_freetext
from screener.core.verify_evidence import verify_evidence
from screener.intake.sanitize_text import sanitize
from screener.intake.validate_file import validate_file
from screener.llm import load_prompt
from screener.llm.judge_resume import PROMPT_NAME, build_user_message, judge_resume
from screener.models import Candidate, Flag, Rubric, ScoredCriterion, now
from screener.ports import LLMClient, ResumeParser

_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class TraceRecord:
    """One judge call, for offline evaluation and prompt regression testing (§17).

    **Contains full resume text.** Whatever consumes this is a second store of
    candidate data and must sit inside the erasure path, or `purge_candidate`
    silently stops working while appearing implemented.
    """

    file_sha256: str
    system: str
    user: str
    output: dict[str, object]
    prompt_tokens: int
    attempts: int


TraceSink = Callable[[TraceRecord], None]


@dataclass(frozen=True)
class Deps:
    """Collaborators, injected. The seam that keeps this module testable."""

    parser: ResumeParser
    llm: LLMClient
    trace: TraceSink | None = None


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
    judged, not as what was queued (§16.2). The cache key depends on this being
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
) -> Candidate:
    """The single shape every failure returns.

    `score=None`, never `0.0`. `review_required=True` unconditionally: something
    went wrong and a person has to see this candidate, whatever the cause.

    `criteria` is carried whenever the failure happened *after* judging. An
    escalated candidate is one a human now has to decide about, and they cannot
    do that from a flag alone — they need the verdicts, the quotes, and the
    `match_ratio` that explains why verification failed. Dropping them would make
    the review queue unactionable, which is the §18.2 failure where oversight
    collapses into rubber-stamping.
    """
    return Candidate(
        run_id=run_id,
        filename=path.name,
        file_sha256=sha,
        score=None,
        band=None,
        must_haves_met=False,
        criteria=criteria or [],
        flags=acc.flags,
        summary=summary,
        scoreable=False,
        review_required=True,
        scored_at=now(),
    )


def screen_one(  # noqa: PLR0911 — one return per terminal stage; collapsing them would hide the order
    path: Path,
    rubric: Rubric,
    deps: Deps,
    *,
    run_id: str,
    root: Path,
) -> Candidate:
    """Screen one resume. Returns a `Candidate` in every case; never raises.

    `root` is the run folder, used to confirm the file has not escaped it. It is
    a parameter rather than a setting because it varies per run, and resolving it
    from configuration here would make containment depend on global state.
    """
    acc = _Accumulator()

    # --- §8.2 file validation, before anything opens the file properly -------
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

    # --- §8.4 sandboxed parse ------------------------------------------------
    parse = deps.parser.parse(path)
    if not parse.ok or parse.parsed is None:
        acc.add(*parse.flags, review=True)
        return _unscoreable(run_id=run_id, path=path, sha=sha, acc=acc, summary=parse.error[:600])

    # --- §8.6 unicode sanitization, before every other text stage ------------
    text, stripped = sanitize(parse.parsed.text)
    if stripped:
        acc.add(Flag.SANITIZED_TEXT)
    if not text.strip():
        # Sanitization removed everything that was there. The document was
        # invisible characters and nothing else.
        acc.add(Flag.EXTRACTION_FAILED, review=True)
        return _unscoreable(run_id=run_id, path=path, sha=sha, acc=acc)

    # --- §10.2 injection detection: escalates, never excludes ---------------
    if settings.injection_detection:
        injection = detect_injection(text)
        if injection.detected:
            acc.add(Flag.SUSPECTED_INJECTION, review=True)

    # --- §10.x redaction. `sent` is what the model sees and what §10.5 matches
    sent = redact_pii(text)[0] if settings.redact_pii else text

    # --- §10.1 budget, against the assembled prompt --------------------------
    system = load_prompt(PROMPT_NAME)
    user = build_user_message(sent, rubric)
    budget = check_budget(deps.llm.count_prompt_tokens(system, user))
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
        )

    # --- §11 + §10.3 judge, with one corrective retry on a verdict-set mismatch
    judged = judge_resume(deps.llm, sent, rubric)
    if judged.output is None or not judged.check.ok:
        acc.add(*judged.flags, review=True)
        return _unscoreable(
            run_id=run_id, path=path, sha=sha, acc=acc, summary=judged.check.describe()
        )

    if deps.trace is not None:
        deps.trace(
            TraceRecord(
                file_sha256=sha,
                system=system,
                user=user,
                output=judged.output.model_dump(),
                prompt_tokens=budget.prompt_tokens,
                attempts=judged.attempts,
            )
        )

    # --- §10.7 free-text screening, before anything is shown or scored -------
    screened = screen_freetext(judged.output)
    if screened.removed:
        acc.add(*screened.flags)

    # --- §10.5 evidence verification, against `sent` -------------------------
    verified = verify_evidence(screened.output, sent, rubric)
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
        )

    # --- §10.4 arithmetic ----------------------------------------------------
    scored = compute_score(verified.criteria, rubric)
    acc.add(*scored.flags, review=scored.review_required)

    return Candidate(
        run_id=run_id,
        filename=path.name,
        file_sha256=sha,
        score=scored.score,
        band=assign_band(scored.score),
        must_haves_met=scored.must_haves_met,
        criteria=verified.criteria,
        notable_strengths=screened.output.notable_strengths,
        red_flags=screened.output.red_flags,
        summary=screened.output.summary,
        flags=acc.flags,
        scoreable=True,
        review_required=acc.review_required,
        scored_at=now(),
    )


def screen_batch(
    paths: list[Path],
    rubric: Rubric,
    deps: Deps,
    *,
    run_id: str,
    root: Path,
) -> list[Candidate]:
    """Screen a list of files serially. The break-glass path for `cli.py` (§16).

    Serial, not concurrent: `OLLAMA_NUM_PARALLEL=1` means parallel calls would
    queue at Ollama anyway, and concurrency here would only add a way to lose
    results.

    Deliberately has **no queue, no resumption, and no persistence** — the worker
    owns those. This exists so a batch can be run with the API and database in an
    unknown state, which is exactly when the queue is not available to lean on.
    """
    return [screen_one(path, rubric, deps, run_id=run_id, root=root) for path in paths]
