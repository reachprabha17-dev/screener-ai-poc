"""Reproducibility measurement (spec 10.8, build gate 20 step 19).

`temperature=0` does **not** give bit-identical output from llama.cpp. Parallel
slot assignment, KV-cache reuse and float reduction order on GPU all vary. So the
claim this system makes is not bit-equality — it is a **measured rate**, and this
is what measures it.

**Gate: ≥98% verdict stability, zero band changes.**

Those are two different bars on purpose. A single criterion wobbling between
`strong` and `partial` on one resume is tolerable noise. The same wobble changing
a **band** is a different outcome for a person, and the tolerance for that is
zero — a candidate who is Band A on Monday and Band B on Tuesday, from the same
file and the same rubric, has been treated arbitrarily.

The figure belongs in run metadata (`runs.reproducibility_rate`), so a stored
decision carries the stability of the conditions that produced it.

Run: `python -m eval.run_eval [--repeats 5]`
"""

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import settings
from eval.corpus import Corpus, CorpusError, load
from screener.clients.ollama_client import OllamaClient
from screener.core.compute_score import compute_score
from screener.core.rank import assign_band
from screener.core.verify_evidence import verify_evidence
from screener.llm.judge_resume import judge_resume
from screener.ports import LLMClient

STABILITY_GATE = 0.98
DEFAULT_REPEATS = 5


@dataclass
class Stability:
    verdicts_total: int = 0
    verdicts_stable: int = 0
    band_changes: list[str] = field(default_factory=list)
    bands_by_case: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    scores_by_case: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    failures: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.verdicts_stable / self.verdicts_total if self.verdicts_total else 0.0

    @property
    def passed(self) -> bool:
        return self.rate >= STABILITY_GATE and not self.band_changes


def measure(llm: LLMClient, corpus: Corpus, repeats: int) -> Stability:
    """Re-score every case `repeats` times and compare against the modal answer.

    Modal rather than first-run: comparing to run #1 would make a single
    anomalous first pass look like total instability, which misreports the thing
    being measured.
    """
    result = Stability()

    for case in corpus.cases:
        rubric = case.rubric()
        runs: list[dict[str, str]] = []

        for _ in range(repeats):
            try:
                judged = judge_resume(llm, case.resume, rubric)
            except Exception as exc:  # noqa: BLE001 — a failed call is not an unstable verdict
                result.failures.append(f"{case.id}: {type(exc).__name__}: {exc}")
                continue
            if judged.output is None:
                result.failures.append(f"{case.id}: {judged.check.describe()}")
                continue

            runs.append({c.id: c.verdict for c in judged.output.criteria})

            # Band is what a reviewer sees, so stability has to be measured on
            # the band — not on the float behind it (10.6).
            verified = verify_evidence(judged.output, case.resume, rubric)
            if verified.scoreable and verified.criteria:
                scored = compute_score(verified.criteria, rubric)
                result.bands_by_case[case.id][str(assign_band(scored.score))] += 1
                result.scores_by_case[case.id].append(scored.score)
            else:
                result.bands_by_case[case.id]["unscoreable"] += 1

        if not runs:
            continue

        for criterion in rubric.criteria:
            seen = Counter(run.get(criterion.id, "missing") for run in runs)
            result.verdicts_total += len(runs)
            result.verdicts_stable += max(seen.values())

        bands = result.bands_by_case[case.id]
        if len(bands) > 1:
            result.band_changes.append(f"{case.id}: {dict(bands)}")

    return result


def report(result: Stability, repeats: int, model: str) -> int:
    print(f"\nmodel               {model}")
    print(f"repeats per case    {repeats}")
    print(f"seed                {settings.seed}  num_ctx {settings.num_ctx}")
    print(f"\nverdict stability   {result.rate:.1%}  (gate ≥{STABILITY_GATE:.0%})")
    print(f"band changes        {len(result.band_changes)}  (gate 0)")

    for change in result.band_changes:
        print(f"  UNSTABLE  {change}")

    spread = {
        case: round(max(scores) - min(scores), 2)
        for case, scores in result.scores_by_case.items()
        if len(scores) > 1 and max(scores) != min(scores)
    }
    if spread:
        print("\nscore spread (same input, different runs)")
        for case, delta in sorted(spread.items(), key=lambda kv: -kv[1]):
            print(f"  {case:<24} ±{delta}")
    else:
        print("\nscore spread        none — identical scores across all repeats")

    if result.failures:
        print(f"\nfailed calls ({len(result.failures)})")
        for failure in result.failures[:10]:
            print(f"  {failure}")

    if result.passed:
        print("\nPASS")
        return 0
    print("\nFAIL — record this rate in run metadata and do not claim reproducibility")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--model", default=settings.judge_model)
    parser.add_argument("--corpus", type=Path, default=None)
    args = parser.parse_args(argv)

    settings.judge_model = args.model
    try:
        corpus = load(args.corpus)
    except CorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = measure(OllamaClient(), corpus, args.repeats)
    exit_code = report(result, args.repeats, args.model)

    if result.scores_by_case:
        means = [statistics.mean(s) for s in result.scores_by_case.values() if s]
        if means:
            print(f"mean score across corpus: {statistics.mean(means):.2f}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
