"""The labelled set and its provenance (spec 18.1).

**An accuracy number inherits its labeller's judgement entirely.** Reporting one
without saying who produced the labels and how is not a measurement — it is a
number with the disagreement hidden inside it. 18.1 sets the floor: two
independent labellers, inter-rater agreement reported *alongside* accuracy,
disagreements adjudicated and the disagreement retained.

So the loader **refuses to yield a corpus that cannot say where its labels came
from**. That is deliberately obstructive. A harness that silently accepts an
unlabelled file is one that will eventually be pointed at real hiring decisions
and produce a confident percentage nobody can defend.

Agreement is Cohen's κ, computed here rather than pulled from scikit-learn: it is
six lines over three verdict values, and vendoring a framework onto an
air-gapped box for that is the trade 22.2 already rejected for tokenizers.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from screener.models import Criterion, Rubric, Verdict

DEFAULT_PATH = Path(__file__).resolve().parent / "labelled_set.jsonl"

VERDICTS: tuple[Verdict, ...] = ("strong", "partial", "none")


class CorpusError(RuntimeError):
    """The corpus cannot support the claim someone is about to make from it."""


@dataclass(frozen=True)
class LabelledCase:
    """One resume judged against one rubric, with human labels attached."""

    id: str
    resume: str
    criteria: list[Criterion]
    # labeller id -> {criterion id -> verdict}
    labels: dict[str, dict[str, Verdict]]
    adjudicated: dict[str, Verdict]
    notes: str = ""

    def rubric(self) -> Rubric:
        return Rubric(
            id=f"rub-{self.id}",
            position_id=f"pos-{self.id}",
            version=1,
            created_by="eval",
            criteria=self.criteria,
        )


@dataclass(frozen=True)
class Provenance:
    """Who labelled this and how. Required — see the module docstring."""

    labellers: list[str]
    method: str
    adjudication: str
    caveats: str = ""

    @property
    def is_independent(self) -> bool:
        """18.1's floor: at least two people who did not see each other's work."""
        return len(self.labellers) >= 2


@dataclass(frozen=True)
class Corpus:
    provenance: Provenance
    cases: list[LabelledCase] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.cases)

    @property
    def judgements(self) -> int:
        return sum(len(c.adjudicated) for c in self.cases)


def load(path: Path | None = None) -> Corpus:
    """Read the corpus, refusing anything that cannot state its provenance.

    The first line of the file is a header object describing the labelling; every
    subsequent line is a case.
    """
    target = path or DEFAULT_PATH
    if not target.is_file():
        raise CorpusError(
            f"No labelled set at {target}. Accuracy cannot be reported without one — "
            "see 18.1 for what it must contain."
        )

    lines = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise CorpusError(f"{target} is empty.")

    header = json.loads(lines[0])
    if header.get("kind") != "provenance":
        raise CorpusError(
            f"{target} does not begin with a provenance header. An accuracy figure "
            "inherits its labeller's judgement entirely, so the labelling must be "
            "declared before any case (18.1)."
        )

    provenance = Provenance(
        labellers=list(header.get("labellers", [])),
        method=str(header.get("method", "")),
        adjudication=str(header.get("adjudication", "")),
        caveats=str(header.get("caveats", "")),
    )
    if not provenance.labellers or not provenance.method:
        raise CorpusError("The provenance header must name its labellers and method.")

    cases = [_case(json.loads(line), index) for index, line in enumerate(lines[1:], start=2)]
    return Corpus(provenance=provenance, cases=cases)


def _case(raw: dict[str, Any], line_no: int) -> LabelledCase:
    try:
        criteria = [Criterion(**c) for c in raw["criteria"]]
        labels = {
            labeller: {cid: _verdict(v) for cid, v in verdicts.items()}
            for labeller, verdicts in raw.get("labels", {}).items()
        }
        adjudicated = {cid: _verdict(v) for cid, v in raw["adjudicated"].items()}
    except (KeyError, TypeError, ValueError) as exc:
        raise CorpusError(f"line {line_no}: {exc}") from exc

    missing = {c.id for c in criteria} - set(adjudicated)
    if missing:
        raise CorpusError(f"line {line_no}: no adjudicated label for {sorted(missing)}")

    return LabelledCase(
        id=str(raw["id"]),
        resume=str(raw["resume"]),
        criteria=criteria,
        labels=labels,
        adjudicated=adjudicated,
        notes=str(raw.get("notes", "")),
    )


def _verdict(value: str) -> Verdict:
    if value not in VERDICTS:
        raise ValueError(f"unknown verdict {value!r}")
    return value  # type: ignore[return-value]


# --- inter-rater agreement ---------------------------------------------------


def cohens_kappa(a: list[str], b: list[str]) -> float:
    """Agreement between two labellers, corrected for chance.

    Raw percentage agreement flatters a corpus where one verdict dominates: two
    labellers who both answer `none` to everything agree 100% of the time and
    have demonstrated nothing. κ subtracts the agreement you would expect from
    their marginal frequencies alone.

    Returns 1.0 for perfect agreement, 0.0 for chance, negative for worse than
    chance. Conventionally: >0.8 strong, 0.6–0.8 moderate, <0.6 means the
    *criteria* are ambiguous — which is a finding about the rubric, not the
    labellers.
    """
    if not a or len(a) != len(b):
        raise ValueError("kappa needs two equal, non-empty label sequences")

    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n

    count_a, count_b = Counter(a), Counter(b)
    expected = sum((count_a[v] / n) * (count_b[v] / n) for v in set(a) | set(b))

    if expected == 1.0:
        # Both labellers used exactly one value. Agreement is real but carries no
        # information; saying so beats dividing by zero.
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def agreement_report(corpus: Corpus) -> dict[str, Any]:
    """Pairwise κ across every labeller pair, plus where they disagreed.

    Disagreements are retained rather than averaged away (18.1): a criterion two
    experienced people read differently is a criterion the model cannot be
    expected to read consistently either.
    """
    labellers = sorted(corpus.provenance.labellers)
    pairs: dict[str, float] = {}
    disagreements: list[dict[str, Any]] = []

    for i, first in enumerate(labellers):
        for second in labellers[i + 1 :]:
            left: list[str] = []
            right: list[str] = []
            for case in corpus.cases:
                if first not in case.labels or second not in case.labels:
                    continue
                for criterion in case.criteria:
                    lv = case.labels[first].get(criterion.id)
                    rv = case.labels[second].get(criterion.id)
                    if lv is None or rv is None:
                        continue
                    left.append(lv)
                    right.append(rv)
                    if lv != rv:
                        disagreements.append(
                            {
                                "case": case.id,
                                "criterion": criterion.id,
                                "text": criterion.text,
                                first: lv,
                                second: rv,
                                "adjudicated": case.adjudicated.get(criterion.id),
                            }
                        )
            if left:
                pairs[f"{first} vs {second}"] = round(cohens_kappa(left, right), 3)

    return {
        "labellers": labellers,
        "independent": corpus.provenance.is_independent,
        "pairwise_kappa": pairs,
        "disagreements": disagreements,
    }
