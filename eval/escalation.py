"""Escalation rate and its drivers (spec 18.2, 18.2.1).

**The escalation rate is a design budget, not an emergent property.** Human
oversight collapses into rubber-stamping the moment the review queue exceeds what
a person will actually read — and that is the control failing *silently*, while
still appearing to work. Target <3%.

So this reports the rate **and the breakdown**, because the rate alone tells you
nothing you can act on. "12% need review" prompts a shrug; "9 of 12 are
`EVIDENCE_UNVERIFIED`, and 7 of those 9 had a perfect match ratio and failed only
on quote length" prompts a fix.

`--sweep` exists for 18.2.1 specifically. `evidence_match_min_chars` was
inherited from the rejected `or 25 chars` clause (22.1), where 25 was a
*sufficient* condition — "verified if at least 25 chars matched". Reusing it as a
*necessary* condition was never measured. This replays the persisted
`match_ratio` / `longest_span` / matched-character figures against candidate
thresholds so the decision is made from data rather than from where the number
happened to come from.

Run: `python -m eval.escalation [--sweep]`
"""

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import settings
from eval.corpus import Corpus, CorpusError, load
from screener.clients.ollama_client import OllamaClient
from screener.core.compute_score import compute_score
from screener.core.screen_freetext import screen_freetext
from screener.core.verify_evidence import align, evidence_mentions_criterion, verify_evidence
from screener.models import Flag
from screener.ports import LLMClient

# Thresholds to try in a sweep. 25 is the current value; the rest bracket it.
SWEEP_CHARS = (12, 16, 18, 20, 25, 30)


@dataclass
class Escalations:
    candidates: int = 0
    escalated: int = 0
    drivers: Counter[str] = field(default_factory=Counter)
    # (criterion text, evidence, ratio, span, matched_chars, relevant)
    evidence_stats: list[tuple[str, str, float, int, int, bool]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.escalated / self.candidates if self.candidates else 0.0


def measure(llm: LLMClient, corpus: Corpus) -> Escalations:
    """Run the real 10.5 → 10.4 path over the corpus.

    Deliberately not the full `screen_one`: the corpus holds resume *text*, not
    files, so intake and parsing are not exercised. Their failure modes are
    covered by the step 5 and step 8 gates; what is under measurement here is the
    escalation the *judging* path produces.
    """
    result = Escalations()

    for case in corpus.cases:
        rubric = case.rubric()
        try:
            judged = _judge(llm, case.resume, rubric)
        except Exception as exc:  # noqa: BLE001
            result.failures.append(f"{case.id}: {type(exc).__name__}: {exc}")
            continue
        if judged is None:
            result.candidates += 1
            result.escalated += 1
            result.drivers[Flag.VERDICT_SET_MISMATCH.value] += 1
            continue

        screened = screen_freetext(judged)
        verified = verify_evidence(screened.output, case.resume, rubric)

        # Record every quote's numbers so a threshold sweep has something real.
        by_id = {c.id: c for c in screened.output.criteria}
        for criterion in rubric.criteria:
            cv = by_id.get(criterion.id)
            if cv is None or cv.verdict == "none":
                continue
            alignment = align(cv.evidence, case.resume)
            result.evidence_stats.append(
                (
                    criterion.text,
                    cv.evidence,
                    alignment.ratio,
                    alignment.longest_span,
                    alignment.matched_chars,
                    evidence_mentions_criterion(criterion.text, cv.evidence),
                )
            )

        result.candidates += 1
        flags = list(verified.flags)
        review = verified.review_required

        if verified.scoreable and verified.criteria:
            scored = compute_score(verified.criteria, rubric)
            flags += scored.flags
            review = review or scored.review_required

        if review or not verified.scoreable:
            result.escalated += 1
            for flag in flags:
                if flag is not Flag.MISSING_MUST_HAVE:  # not an escalation (10.4)
                    result.drivers[flag.value] += 1
            if not flags:
                result.drivers["partial_must_have"] += 1

    return result


def _judge(llm: LLMClient, resume: str, rubric: object):  # noqa: ANN202, ANN001
    from screener.llm.judge_resume import judge_resume

    judged = judge_resume(llm, resume, rubric)  # type: ignore[arg-type]
    return judged.output if judged.check.ok else None


def sweep(result: Escalations) -> None:
    """What would change if `evidence_match_min_chars` moved? (18.2.1)

    Reports, for each candidate threshold, how many quotes would fail — split by
    whether they also failed the ratio gate. A quote at `ratio = 1.00` failing
    only on length is a *correct* quote being escalated, which is the false
    positive the budget cannot afford.
    """
    print("\nthreshold sweep — evidence_match_min_chars")
    print(f"  quotes examined: {len(result.evidence_stats)}")
    print(f"  {'chars':>6}  {'would fail':>10}  {'of those, ratio=1.00':>21}")

    for candidate in SWEEP_CHARS:
        failing = [
            s
            for s in result.evidence_stats
            if not (
                s[2] >= settings.evidence_match_ratio
                and s[3] >= settings.evidence_min_block_tokens
                and s[4] >= candidate
            )
        ]
        perfect = [s for s in failing if s[2] >= 1.0]
        marker = "  <- current" if candidate == settings.evidence_match_min_chars else ""
        print(f"  {candidate:>6}  {len(failing):>10}  {len(perfect):>21}{marker}")

    perfect_but_short = sorted(
        (
            s
            for s in result.evidence_stats
            if s[2] >= 1.0 and s[4] < settings.evidence_match_min_chars
        ),
        key=lambda s: s[4],
    )
    if perfect_but_short:
        print("\n  exact quotes rejected only for being short:")
        for _text, evidence, _ratio, _span, chars, _rel in perfect_but_short:
            print(f"    {chars:>3} chars  {evidence[:60]!r}")


def report(result: Escalations) -> int:
    budget = settings.escalation_budget
    stated = "unset" if budget is None else f"{budget:.0%}"
    print(f"\ncandidates          {result.candidates}")
    print(f"needing review      {result.escalated}  =  {result.rate:.0%}  (budget {stated})")

    if result.drivers:
        print("\ndrivers")
        for flag, count in result.drivers.most_common():
            print(f"  {count:>3}  {flag}")

    irrelevant = sum(1 for s in result.evidence_stats if not s[5])
    if irrelevant:
        print(f"\n  {irrelevant} quote(s) failed the 10.5(c) relevance check")

    if result.failures:
        print(f"\nfailed ({len(result.failures)})")
        for failure in result.failures[:10]:
            print(f"  {failure}")

    if budget is None:
        # This report is where the number is supposed to come from (19.2), so an
        # unset budget is the expected state on the first run — and a pass/fail
        # against a guess would be worse than no verdict at all.
        print(
            f"\nNO BUDGET SET — this run measured {result.rate:.0%}. Set escalation_budget "
            "from it and record escalation_budget_source_run."
        )
        return 0

    if result.rate <= budget:
        print("\nWITHIN BUDGET")
        return 0
    print(
        f"\nOVER BUDGET — {result.rate:.0%} against {budget:.0%}. The answer is fewer, "
        "better-targeted escalations, never a bigger queue (18.2)."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=settings.judge_model)
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--sweep", action="store_true", help="18.2.1 threshold analysis")
    args = parser.parse_args(argv)

    settings.judge_model = args.model
    try:
        corpus = load(args.corpus)
    except CorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = measure(OllamaClient(), corpus)
    exit_code = report(result)
    if args.sweep:
        sweep(result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
