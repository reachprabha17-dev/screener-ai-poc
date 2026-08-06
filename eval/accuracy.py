"""Accuracy against the labelled set (spec 18.1).

**Reports agreement before accuracy, every time.** An accuracy figure inherits
its labeller's judgement entirely, so quoting one without the inter-rater number
beside it hides the disagreement inside a percentage. Where the corpus has fewer
than two independent labellers this says so loudly rather than omitting the line.

**Error direction matters more than the headline number.** A model that
under-credits rejects people who should have advanced; one that over-credits
advances people who should not have. Those are different harms with different
owners, and a single accuracy percentage conceals which one you have. The
confusion matrix and the `strong`↔`none` count are the numbers to argue about.

Run: `python -m eval.accuracy [--model granite4.1:8b]`
"""

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import settings
from eval.corpus import Corpus, CorpusError, agreement_report, load
from screener.clients.ollama_client import OllamaClient
from screener.llm.judge_resume import judge_resume
from screener.models import Verdict
from screener.ports import LLMClient

# A one-step error (strong↔partial) is a calibration difference. A two-step error
# is the model asserting evidence exists where there is none, or missing it
# entirely — the two outcomes a person would actually contest.
SEVERE = ({"strong", "none"},)


@dataclass
class Result:
    total: int = 0
    correct: int = 0
    severe: int = 0
    confusion: Counter[tuple[str, str]] = field(default_factory=Counter)
    misses: list[dict[str, Any]] = field(default_factory=list)
    unjudged: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def over_credited(self) -> int:
        """Model said more than the label. Advances someone who should not be."""
        order = {"none": 0, "partial": 1, "strong": 2}
        return sum(n for (want, got), n in self.confusion.items() if order[got] > order[want])

    @property
    def under_credited(self) -> int:
        """Model said less than the label. Rejects someone who should have advanced."""
        order = {"none": 0, "partial": 1, "strong": 2}
        return sum(n for (want, got), n in self.confusion.items() if order[got] < order[want])


def score(llm: LLMClient, corpus: Corpus) -> Result:
    result = Result()

    for case in corpus.cases:
        try:
            judged = judge_resume(llm, case.resume, case.rubric())
        except Exception as exc:  # noqa: BLE001 — one bad case must not lose the run
            result.unjudged.append(f"{case.id}: {type(exc).__name__}: {exc}")
            continue

        if judged.output is None:
            result.unjudged.append(f"{case.id}: {judged.check.describe()}")
            continue

        got = {c.id: c.verdict for c in judged.output.criteria}
        for criterion in case.criteria:
            want: Verdict = case.adjudicated[criterion.id]
            actual = got.get(criterion.id)
            if actual is None:
                result.unjudged.append(f"{case.id}/{criterion.id}: no verdict returned")
                continue

            result.total += 1
            result.confusion[(want, actual)] += 1
            if actual == want:
                result.correct += 1
            else:
                if {want, actual} in SEVERE:
                    result.severe += 1
                result.misses.append(
                    {
                        "case": case.id,
                        "criterion": criterion.id,
                        "text": criterion.text,
                        "want": want,
                        "got": actual,
                        "must_have": criterion.must_have,
                    }
                )

    return result


def report(corpus: Corpus, result: Result, model: str) -> int:
    agreement = agreement_report(corpus)

    print(f"\nmodel        {model}")
    print(f"corpus       {len(corpus)} cases, {corpus.judgements} judgements")
    print(f"labellers    {', '.join(agreement['labellers'])}")

    # Agreement first, deliberately (18.1).
    if agreement["independent"]:
        for pair, kappa in agreement["pairwise_kappa"].items():
            print(f"  kappa      {pair}: {kappa}")
        if agreement["disagreements"]:
            print(f"  disagreed  {len(agreement['disagreements'])} judgement(s), retained")
    else:
        print(
            "  kappa      NOT AVAILABLE — fewer than two independent labellers.\n"
            "             Accuracy below is a regression signal, not evidence about\n"
            "             real applications (18.1)."
        )
    if corpus.provenance.caveats:
        print(f"\n  caveats    {corpus.provenance.caveats[:300]}")

    print(f"\naccuracy     {result.correct}/{result.total} = {result.accuracy:.0%}")
    print(f"severe       {result.severe}  (strong<->none)")
    print(f"over-credit  {result.over_credited}   under-credit {result.under_credited}")

    print("\nconfusion (want -> got)")
    for want in ("strong", "partial", "none"):
        row = "  ".join(
            f"{got}:{result.confusion[(want, got)]:<3}" for got in ("strong", "partial", "none")
        )
        print(f"  {want:<8} {row}")

    if result.misses:
        print("\nmisses")
        for miss in result.misses:
            flag = " MUST-HAVE" if miss["must_have"] else ""
            print(
                f"  {miss['case']:<20} {miss['criterion']}  want={miss['want']:<8} "
                f"got={miss['got']:<8}{flag}  {miss['text'][:40]}"
            )

    if result.unjudged:
        print(f"\nunjudged ({len(result.unjudged)})")
        for entry in result.unjudged:
            print(f"  {entry}")

    return 0 if result.severe == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=settings.chat_model)
    parser.add_argument("--corpus", type=Path, default=None)
    args = parser.parse_args(argv)

    settings.chat_model = args.model
    try:
        corpus = load(args.corpus)
    except CorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return report(corpus, score(OllamaClient(), corpus), args.model)


if __name__ == "__main__":
    raise SystemExit(main())
