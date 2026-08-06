"""The evaluation harness (spec 18, build gate 20 step 19).

The harness is the thing that decides whether this system is allowed to be
trusted, so its own correctness matters more than most. Two properties get the
most attention:

**It refuses to report accuracy without provenance.** Deliberately obstructive.
A harness that silently accepts an unlabelled file will eventually be pointed at
real hiring decisions and produce a confident percentage nobody can defend.

**κ is computed, not approximated.** Raw agreement flatters a corpus where one
verdict dominates — two labellers who both answer `none` to everything agree
100% of the time and have demonstrated nothing.

The measurement scripts themselves run against fakes here; their real output is
the `live` runs recorded in 18.2.1 and 10.8.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from eval.accuracy import Result, score
from eval.corpus import DEFAULT_PATH, CorpusError, agreement_report, cohens_kappa, load
from eval.escalation import measure as measure_escalation
from eval.run_eval import STABILITY_GATE
from eval.run_eval import measure as measure_stability


class FakeLLM:
    """Returns a scripted verdict for every criterion."""

    def __init__(self, verdict: str = "strong", evidence: str | None = None) -> None:
        self._verdict = verdict
        self._evidence = evidence

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        ids = [
            line.split(":", 1)[0].strip()
            for line in user.splitlines()
            if line.startswith("C") and ":" in line
        ]
        return {
            "criteria": [
                {
                    "id": cid,
                    "verdict": self._verdict,
                    "evidence": self._evidence or "Owned the Kubernetes platform for 12 services",
                }
                for cid in ids
            ],
            "summary": "",
            "notable_strengths": [],
            "red_flags": [],
        }

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def count_prompt_tokens(self, system: str, user: str) -> int:
        return 900

    def health(self) -> bool:
        return True

    @property
    def model_digest(self) -> str:
        return "sha256:fake"


# --- the shipped corpus ------------------------------------------------------


def test_the_shipped_corpus_loads_and_declares_itself() -> None:
    corpus = load()

    assert len(corpus) >= 10
    assert corpus.provenance.labellers
    assert corpus.provenance.method
    assert corpus.provenance.caveats, "a corpus must state what it is not"


def test_the_seed_corpus_admits_it_is_not_a_substitute() -> None:
    """18.1 requires two independent labellers.

    The seed set has one, and says so in terms nobody can quote past. A corpus
    that overstates itself is worse than none, because the number it produces
    gets used.
    """
    corpus = load()

    assert corpus.provenance.is_independent is False
    assert "NOT A SUBSTITUTE" in corpus.provenance.caveats
    assert "two" in corpus.provenance.caveats.casefold()


def test_the_corpus_covers_the_verified_failure_modes() -> None:
    """Every 1 failure mode that reached a candidate needs a regression case."""
    ids = {case.id for case in load().cases}

    for required in (
        "injection-attempt",  # the verified live attack
        "security-engineer",  # detection must escalate, never exclude
        "absent-technology",  # verified-but-irrelevant evidence
        "dates-not-years",  # phrase-sensitivity on duration
        "terse-bullets",  # formatting must not cost a verdict
    ):
        assert required in ids, required


def test_every_criterion_has_an_adjudicated_label() -> None:
    for case in load().cases:
        assert set(case.adjudicated) >= {c.id for c in case.criteria}, case.id


# --- provenance is not optional ----------------------------------------------


def test_a_corpus_without_provenance_is_refused(tmp_path: Path) -> None:
    """The obstruction is the point."""
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"id": "x", "resume": "r", "criteria": [], "adjudicated": {}}))

    with pytest.raises(CorpusError, match="provenance"):
        load(path)


def test_a_corpus_without_labellers_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"kind": "provenance", "labellers": [], "method": "guessing"}))

    with pytest.raises(CorpusError, match="labellers"):
        load(path)


def test_a_missing_corpus_says_why_it_matters(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="18.1"):
        load(tmp_path / "nothing.jsonl")


def test_an_unlabelled_criterion_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        json.dumps({"kind": "provenance", "labellers": ["a", "b"], "method": "m"})
        + "\n"
        + json.dumps(
            {
                "id": "x",
                "resume": "r",
                "criteria": [{"id": "C1", "text": "t", "weight": 1}],
                "adjudicated": {},
            }
        )
    )

    with pytest.raises(CorpusError, match="C1"):
        load(path)


# --- inter-rater agreement ---------------------------------------------------


def test_kappa_is_one_for_perfect_agreement() -> None:
    assert cohens_kappa(["strong", "none", "partial"], ["strong", "none", "partial"]) == 1.0


def test_kappa_is_zero_at_chance() -> None:
    """Two labellers agreeing exactly as often as their marginals predict."""
    a = ["strong", "strong", "none", "none"]
    b = ["strong", "none", "strong", "none"]

    assert cohens_kappa(a, b) == pytest.approx(0.0, abs=0.01)


def test_kappa_punishes_a_degenerate_corpus() -> None:
    """The reason raw agreement is not enough.

    Both labellers answered `none` to everything. They agree 100% of the time and
    have demonstrated nothing about their ability to tell verdicts apart.
    """
    a = ["none"] * 10
    b = ["none"] * 10

    assert cohens_kappa(a, b) == 1.0  # perfect, but...
    # ...the corpus itself carries no discriminating signal, which the report
    # surfaces via the label distribution rather than by penalising kappa.
    assert len(set(a)) == 1


def test_kappa_rejects_mismatched_sequences() -> None:
    with pytest.raises(ValueError, match="equal"):
        cohens_kappa(["strong"], ["strong", "none"])


def test_agreement_reports_disagreements_rather_than_averaging_them(tmp_path: Path) -> None:
    """A criterion two people read differently is a finding about the *rubric*."""
    path = tmp_path / "two.jsonl"
    path.write_text(
        json.dumps({"kind": "provenance", "labellers": ["ana", "ben"], "method": "independent"})
        + "\n"
        + json.dumps(
            {
                "id": "case-1",
                "resume": "Deployed to Kubernetes using Helm charts the platform team maintains.",
                "criteria": [{"id": "C1", "text": "Kubernetes in production", "weight": 1}],
                "labels": {"ana": {"C1": "partial"}, "ben": {"C1": "strong"}},
                "adjudicated": {"C1": "partial"},
            }
        )
    )

    report = agreement_report(load(path))

    assert report["independent"] is True
    assert len(report["disagreements"]) == 1
    assert report["disagreements"][0]["criterion"] == "C1"
    assert report["disagreements"][0]["adjudicated"] == "partial"


# --- accuracy ----------------------------------------------------------------


def test_accuracy_separates_over_and_under_crediting() -> None:
    """A single percentage conceals which harm you have.

    Under-crediting rejects someone who should have advanced; over-crediting
    advances someone who should not have. Different harms, different owners.
    """
    result = Result()
    result.confusion.update(
        {("strong", "partial"): 3, ("none", "partial"): 2, ("partial", "partial"): 5}
    )

    assert result.under_credited == 3
    assert result.over_credited == 2


def test_a_perfect_model_scores_perfectly() -> None:
    corpus = load()
    # Only the cases whose every adjudicated verdict is `strong`.
    strong_only = [c for c in corpus.cases if set(c.adjudicated.values()) == {"strong"}]
    assert strong_only, "the corpus should contain at least one all-strong case"

    result = score(FakeLLM("strong"), type(corpus)(provenance=corpus.provenance, cases=strong_only))

    assert result.accuracy == 1.0
    assert result.severe == 0


def test_severe_errors_are_counted_separately() -> None:
    """`strong`↔`none` is the model asserting evidence that is not there, or
    missing it entirely — the outcomes a person would actually contest."""
    corpus = load()
    none_only = [c for c in corpus.cases if set(c.adjudicated.values()) == {"none"}]
    assert none_only

    result = score(FakeLLM("strong"), type(corpus)(provenance=corpus.provenance, cases=none_only))

    assert result.severe == result.total
    assert result.accuracy == 0.0


# --- reproducibility ---------------------------------------------------------


def test_a_deterministic_model_is_perfectly_stable() -> None:
    corpus = load()
    subset = type(corpus)(provenance=corpus.provenance, cases=corpus.cases[:2])

    result = measure_stability(FakeLLM("strong"), subset, repeats=3)

    assert result.rate == 1.0
    assert result.band_changes == []
    assert result.passed


def test_the_stability_gate_is_the_spec_figure() -> None:
    assert STABILITY_GATE == 0.98


# --- escalation --------------------------------------------------------------


def test_escalation_reports_its_drivers_not_just_a_rate() -> None:
    """ "12% need review" prompts a shrug; naming the driver prompts a fix."""
    corpus = load()
    subset = type(corpus)(provenance=corpus.provenance, cases=corpus.cases[:3])

    # Evidence that is real but about nothing in these rubrics.
    result = measure_escalation(
        FakeLLM("strong", evidence="Fluent in Finnish and Estonian"), subset
    )

    assert result.candidates == 3
    assert result.drivers, "an escalation with no named driver is unactionable"


def test_a_missing_must_have_is_not_counted_as_an_escalation() -> None:
    """10.4: the partition *is* the outcome.

    Counting it here would restate the bug that drove the rate to 100%.
    """
    corpus = load()
    subset = type(corpus)(provenance=corpus.provenance, cases=corpus.cases[:3])

    result = measure_escalation(FakeLLM("none"), subset)

    assert "MISSING_MUST_HAVE" not in result.drivers


def test_the_shipped_corpus_path_is_where_the_spec_says() -> None:
    assert DEFAULT_PATH.name == "labelled_set.jsonl"
    assert DEFAULT_PATH.parent.name == "eval"
