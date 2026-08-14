"""difflib only vs judge only vs both, over the labelled set (spec 19.3).

**This experiment decides `verify_scope` and the escalation budget, and both are
currently guesses.** Phase 2 costs ~5 s of GPU per resume and puts more people in
front of a reviewer; whether that buys anything is a measurement nobody has taken.
Running the second model because it sounds prudent is how a review queue becomes
long enough that people stop reading it — which is the oversight control failing
silently while still appearing to work (18.2).

**Cheap because `sent_text` and `match_blocks` are stored.** One judge call and
one verifier call per case; everything else is pure Python replayed three ways.

Two things about the setup are worth being explicit about, because they are the
difference between a comparison and a strawman.

**Stage B's *alignment* runs in all three configurations.** It is pure, free, and
the verifier needs the match blocks to know which part of the resume to quote
back as context. What varies is whose answer *escalates*: stage B's, the second
model's, or either. That is the actual decision on the table — not whether to
compute a ratio.

**One batched verifier call serves both model-using configurations.** `judge
only` would send every in-scope criterion with a quote; `both` would send only
those that also cleared stage B. Sending the superset once and reading back the
subset assumes a criterion's check does not depend on which others shared its
batch. That assumption is not free — it is a batched prompt — but two calls would
double the cost and add sampling variance between the rows being compared, which
would be the worse error.

Absence checking (10.6 B) is deliberately out of scope: the table is about
whether a quote supports a claim, and adding a second axis would make three rows
into nine without answering the question that blocks production.

Definitions, since 19.3 names the columns but not their meaning:

  * A **fabrication** is a non-`none` verdict on a criterion the adjudicators
    labelled `none`. The document does not support the claim and the model
    asserted it anyway, with a quote attached.
  * A **missed fabrication** is one no configuration flag caught — it lands in
    the reviewer's "looks fine" pile. This is the column that matters.
  * A **false escalation** is a flag raised on a criterion whose verdict already
    matched the adjudicated label. Correct work sent to a human, paid for in the
    only currency that is actually scarce.

Run: `python -m eval.compare_verifiers [--scope all|must_have_and_borderline]`
"""

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import settings
from eval.corpus import Corpus, CorpusError, LabelledCase, load
from screener.clients.ollama_client import OllamaClient
from screener.core.screen_freetext import screen_freetext
from screener.core.verify_evidence import verify_evidence
from screener.llm.judge_resume import judge_resume
from screener.llm.verify_support import in_scope, verify_support
from screener.models import Rubric, ScoredCriterion, SupportCheck
from screener.ports import LLMClient

# Ordered weakest-to-strongest in what they are allowed to escalate on, which is
# also the order the spec's table lists them in.
CONFIGURATIONS = ("difflib only", "judge only", "difflib + judge")


@dataclass
class Tally:
    """One configuration's results. Counts, not rates — rates are derived."""

    criteria: int = 0
    fabrications: int = 0
    missed_fabrications: int = 0
    false_escalations: int = 0
    candidates: int = 0
    escalated: int = 0
    # (case id, criterion id, what the model said, what the label said)
    misses: list[tuple[str, str, str, str]] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.escalated / self.candidates if self.candidates else 0.0

    @property
    def caught(self) -> int:
        return self.fabrications - self.missed_fabrications


@dataclass
class Comparison:
    tallies: dict[str, Tally] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def tally(self, configuration: str) -> Tally:
        return self.tallies.setdefault(configuration, Tally())


# --- the three escalation rules ---------------------------------------------
#
# Each answers one question: given what stage B found and what the second model
# said, does this criterion go in front of a person?


def _difflib_flags(criterion: ScoredCriterion) -> bool:
    """Stage B alone: the quote could not be found in the document (10.5).

    `model_verdict` rather than `verdict`, because stage B's self-contradiction
    branch rewrites `verdict` to `none` — reading the post-verification value
    would make the rule invisible to itself.
    """
    if criterion.model_verdict == "none":
        return False
    return not criterion.verified or criterion.negation_suspected


def _judge_flags(check: SupportCheck | None) -> bool:
    """The second model alone: it does not read the quote as establishing the claim."""
    return check is not None and check.support != "supported"


def _both_flags(criterion: ScoredCriterion, check: SupportCheck | None) -> bool:
    """The shipped pipeline (10.4, 10.6 A).

    The `verified` guard is not belt-and-braces — it reproduces the real
    ordering. Stage D is skipped once stage B has failed, because asking a second
    model to reason about a quote we could not find invites it to confirm one
    that was never there.
    """
    if _difflib_flags(criterion):
        return True
    return criterion.verified and _judge_flags(check)


# --- measurement -------------------------------------------------------------


def measure(llm: LLMClient, corpus: Corpus) -> Comparison:
    result = Comparison()

    for case in corpus.cases:
        rubric = case.rubric()
        try:
            scored = _judge_and_align(llm, case, rubric)
        except Exception as exc:  # noqa: BLE001 — one bad case must not lose the run
            result.failures.append(f"{case.id}: {type(exc).__name__}: {exc}")
            continue
        if scored is None:
            # 10.3 rejected the response outright. Every configuration would
            # escalate this candidate for the same reason, so it says nothing
            # about verification and is reported separately rather than counted
            # three times.
            result.failures.append(f"{case.id}: verdict set mismatch")
            continue

        checks = _support_checks(llm, scored, rubric, case.resume)

        for configuration in CONFIGURATIONS:
            tally = result.tally(configuration)
            tally.candidates += 1
            escalated = False

            for criterion in scored:
                check = checks.get(criterion.id)
                flagged = _flagged(configuration, criterion, check)
                escalated = escalated or flagged
                _record(tally, case, criterion, flagged)

            if escalated:
                tally.escalated += 1

    return result


def _flagged(configuration: str, criterion: ScoredCriterion, check: SupportCheck | None) -> bool:
    if configuration == "difflib only":
        return _difflib_flags(criterion)
    if configuration == "judge only":
        return _judge_flags(check)
    return _both_flags(criterion, check)


def _record(tally: Tally, case: LabelledCase, criterion: ScoredCriterion, flagged: bool) -> None:
    """Score one criterion against its adjudicated label."""
    truth = case.adjudicated.get(criterion.id)
    if truth is None:
        return  # not labelled; counting it either way would invent a result

    tally.criteria += 1
    fabricated = criterion.model_verdict != "none" and truth == "none"

    if fabricated:
        tally.fabrications += 1
        if not flagged:
            tally.missed_fabrications += 1
            tally.misses.append((case.id, criterion.id, criterion.model_verdict, truth))
    elif flagged and criterion.model_verdict == truth:
        tally.false_escalations += 1


def _judge_and_align(
    llm: LLMClient, case: LabelledCase, rubric: Rubric
) -> list[ScoredCriterion] | None:
    """One judge call, then the pure stage-B pass. Shared by all three rows.

    Judging once per case rather than once per configuration is what makes the
    three rows comparable: a second sampling of the same resume would put judge
    variance inside a table that is supposed to isolate verification.
    """
    judged = judge_resume(llm, case.resume, rubric)
    if judged.output is None or not judged.check.ok:
        return None
    screened = screen_freetext(judged.output)
    return verify_evidence(screened.output, case.resume, rubric).criteria


def _support_checks(
    llm: LLMClient, criteria: list[ScoredCriterion], rubric: Rubric, sent_text: str
) -> dict[str, SupportCheck]:
    """The verifier's answers, keyed by criterion id.

    The superset `judge only` would send — everything in scope carrying a quote,
    whether or not stage B could find it. See the module docstring on why one
    call serves both model-using rows.
    """
    targets = [c for c in criteria if c.evidence and c.model_verdict != "none" and in_scope(c)]
    if not targets:
        return {}
    return {check.id: check for check in verify_support(llm, targets, rubric, sent_text)}


# --- reporting ---------------------------------------------------------------


def report(result: Comparison) -> int:
    if not result.tallies:
        print("no case produced a usable judgment — nothing to compare", file=sys.stderr)
        return 1

    budget = settings.escalation_budget
    stated = "unset" if budget is None else f"{budget:.0%}"
    print(f"\nverify_scope        {settings.verify_scope}")
    print(f"judge / verifier    {settings.judge_model} / {settings.verifier_model}")
    print(f"escalation budget   {stated}")

    header = f"\n  {'configuration':<18}{'missed fab.':>12}{'false esc.':>12}{'esc. rate':>11}"
    print(header)
    print(f"  {'-' * (len(header) - 3)}")
    for configuration in CONFIGURATIONS:
        tally = result.tallies.get(configuration)
        if tally is None:
            continue
        print(
            f"  {configuration:<18}{tally.missed_fabrications:>12}"
            f"{tally.false_escalations:>12}{tally.rate:>10.0%}"
        )

    reference = result.tallies[CONFIGURATIONS[0]]
    print(
        f"\n  {reference.candidates} candidates, {reference.criteria} labelled criteria, "
        f"{reference.fabrications} fabrication(s) present"
    )

    _misses(result)
    _verdict(result, budget)

    if result.failures:
        print(f"\nunusable ({len(result.failures)})")
        for failure in result.failures[:10]:
            print(f"  {failure}")

    return 0


def _misses(result: Comparison) -> None:
    """What each configuration let through.

    Printed per configuration rather than aggregated: a fabrication only `both`
    catches is the entire argument for running two models, and a fabrication
    *nothing* catches is a finding about the criteria, not about verification.
    """
    for configuration in CONFIGURATIONS:
        tally = result.tallies.get(configuration)
        if tally is None or not tally.misses:
            continue
        print(f"\n  missed by {configuration}:")
        for case_id, criterion_id, said, labelled in tally.misses[:10]:
            print(f"    {case_id} {criterion_id}: model said {said!r}, label {labelled!r}")


def _verdict(result: Comparison, budget: float | None) -> None:
    """Read the table out loud, because the table alone invites the wrong read.

    The temptation is to pick the row with the fewest missed fabrications. That
    row is almost always `difflib + judge`, and choosing it on that basis alone
    is how the escalation rate gets set by whichever configuration escalates
    most — the failure 18.2 exists to prevent.
    """
    strongest = min(
        (result.tallies[c] for c in CONFIGURATIONS if c in result.tallies),
        key=lambda t: (t.missed_fabrications, t.false_escalations),
    )
    best_name = next(c for c in CONFIGURATIONS if result.tallies.get(c) is strongest)

    print(f"\n  fewest missed fabrications: {best_name}")

    if budget is None:
        print(
            "\n  NO BUDGET SET — this is the run it is supposed to come from (19.2). "
            "Pick the configuration whose escalation rate a reviewer will actually "
            "read, set escalation_budget from it, and record "
            "escalation_budget_source_run."
        )
        return

    affordable = [
        c for c in CONFIGURATIONS if c in result.tallies and result.tallies[c].rate <= budget
    ]
    if not affordable:
        print(
            f"\n  NO CONFIGURATION IS WITHIN {budget:.0%}. The lever is "
            "verify_scope='must_have_and_borderline' — verify where being wrong "
            "costs most — never a bigger queue (18.2)."
        )
        return
    print(f"  within budget: {', '.join(affordable)}")
    if best_name not in affordable:
        print(
            f"  {best_name} catches the most and is over budget. That trade is a "
            "decision for a person, not a default."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="19.3 verifier comparison")
    parser.add_argument("--judge-model", default=settings.judge_model)
    parser.add_argument("--verifier-model", default=settings.verifier_model)
    parser.add_argument(
        "--scope",
        choices=("all", "must_have_and_borderline"),
        default=settings.verify_scope,
        help="which criteria stage D examines (10.6); the other half of what this decides",
    )
    parser.add_argument("--corpus", type=Path, default=None)
    args = parser.parse_args(argv)

    settings.judge_model = args.judge_model
    settings.verifier_model = args.verifier_model
    settings.verify_scope = args.scope

    try:
        corpus = load(args.corpus)
    except CorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return report(measure(OllamaClient(), corpus))


if __name__ == "__main__":
    raise SystemExit(main())
