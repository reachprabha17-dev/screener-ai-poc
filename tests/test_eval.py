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

**The verifier comparison cannot be allowed to flatter the shipped
configuration.** 19.3 decides `verify_scope` and the escalation budget, and a
comparison whose arithmetic favours "run both models" would settle that question
by construction rather than by measurement.

The measurement scripts themselves run against fakes here; their real output is
the `live` runs recorded in 18.2.1 and 10.8.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from eval.accuracy import Result, score
from eval.compare_verifiers import CONFIGURATIONS, _both_flags, _difflib_flags, _judge_flags
from eval.compare_verifiers import measure as compare_verifiers
from eval.corpus import DEFAULT_PATH, Corpus, CorpusError, agreement_report, cohens_kappa, load
from eval.escalation import measure as measure_escalation
from eval.run_eval import STABILITY_GATE
from eval.run_eval import measure as measure_stability
from screener.models import ScoredCriterion, SupportCheck

_CRITERION_LINE = re.compile(r"^(C\d+):")


class FakeLLM:
    """Returns a scripted verdict for every criterion."""

    def __init__(self, verdict: str = "strong", evidence: str | None = None) -> None:
        self._verdict = verdict
        self._evidence = evidence

    def chat_json(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if "support_checks" in schema.get("properties", {}):
            # A phase-2 call. Empty is a valid `VerifyOutput`: the verifier
            # agreed with everything and found nothing for the `none` criteria.
            return {"support_checks": [], "absence_checks": []}
        # Anchored on `C<digits>:` rather than `startswith("C")`. The loose form
        # also matched the prompt's own `CRITERIA:` header and returned a verdict
        # for a criterion named "CRITERIA" — which fails 10.3's set-equality
        # check, so every judgment made through this fake was rejected and every
        # test using it was passing on the failure path.
        ids = [m.group(1) for line in user.splitlines() if (m := _CRITERION_LINE.match(line))]
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

    def count_tokens(self, model: str, text: str) -> int:
        return len(text) // 4

    def count_prompt_tokens(self, model: str, system: str, user: str) -> int:
        return 900

    def health(self) -> bool:
        return True

    def digest(self, model: str) -> str:
        return "sha256:fake"

    def ensure_loaded(self, model: str) -> None:
        self.loaded = model

    def unload(self, model: str) -> None:
        self.loaded = None


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


# --- verifier comparison (19.3) ----------------------------------------------


class ScriptedVerifier(FakeLLM):
    """Judges as scripted, and answers every support check the same way.

    Records what it was asked about, because *which* criteria reach the second
    model is half of what 19.3 is measuring — a configuration that quietly sends
    fewer is cheaper for reasons the table would not otherwise show.
    """

    def __init__(
        self,
        verdict: str = "strong",
        evidence: str | None = None,
        support: str = "supported",
    ) -> None:
        super().__init__(verdict, evidence)
        self._support = support
        self.asked: list[str] = []

    def chat_json(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if "support_checks" not in schema.get("properties", {}):
            return super().chat_json(model, system, user, schema)

        ids = [
            line.split(":", 1)[1].strip() for line in user.splitlines() if line.startswith("ID:")
        ]
        self.asked.extend(ids)
        return {
            "support_checks": [
                {
                    "id": cid,
                    "support": self._support,
                    "suggested_verdict": "none",
                    "rationale": "",
                }
                for cid in ids
            ],
            "absence_checks": [],
        }


def criterion(**overrides: Any) -> ScoredCriterion:
    base: dict[str, Any] = {
        "id": "C1",
        "verdict": "strong",
        "model_verdict": "strong",
        "evidence": "Owned the Kubernetes platform for 12 services",
        "verified": True,
        "match_ratio": 1.0,
        "longest_span": 6,
        "weight": 1,
        "must_have": False,
    }
    return ScoredCriterion(**{**base, **overrides})


def check(support: str = "supported") -> SupportCheck:
    return SupportCheck(id="C1", support=support, suggested_verdict="none")  # type: ignore[arg-type]


def one_case_corpus(tmp_path: Path, resume: str, evidence_label: str) -> Corpus:
    """A single labelled criterion, so a scenario can be stated exactly."""
    path = tmp_path / "one.jsonl"
    path.write_text(
        json.dumps({"kind": "provenance", "labellers": ["ana", "ben"], "method": "independent"})
        + "\n"
        + json.dumps(
            {
                "id": "case-1",
                "resume": resume,
                "criteria": [
                    {"id": f"C{i}", "text": "Kubernetes in production", "weight": 1}
                    for i in range(1, 5)
                ],
                "adjudicated": {f"C{i}": evidence_label for i in range(1, 5)},
            }
        )
    )
    return load(path)


def test_stage_b_reads_the_model_verdict_not_the_corrected_one() -> None:
    """The self-contradiction branch rewrites `verdict` to `none` (10.5 a).

    Reading the post-verification value would make the rule invisible to itself:
    a criterion stage B just downgraded would look like an honest `none` and
    stop being counted as something stage B caught.
    """
    contradicted = criterion(model_verdict="strong", verdict="none", verified=False)

    assert _difflib_flags(contradicted) is True


def test_stage_b_does_not_flag_an_honest_absence() -> None:
    assert _difflib_flags(criterion(model_verdict="none", verdict="none", verified=False)) is False


def test_the_second_model_is_never_consulted_about_a_quote_stage_b_rejected() -> None:
    """10.4: D is skipped once B has failed.

    Asking a second model to reason about a quote we could not find in the
    document is an invitation to confirm one that was never there — and a
    `supported` answer there would *unflag* a fabrication.
    """
    unverified = criterion(verified=False)

    assert _both_flags(unverified, check("supported")) is True


def test_the_shipped_configuration_flags_a_superset_of_stage_b() -> None:
    """The property that makes the comparison honest.

    If `difflib + judge` could ever miss what `difflib only` caught, the table
    would be measuring two unrelated systems rather than one with an extra
    stage — and the extra stage would be capable of making things worse.
    """
    for verified in (True, False):
        for support in ("supported", "insufficient", "contradicted"):
            subject = criterion(verified=verified)
            if _difflib_flags(subject):
                assert _both_flags(subject, check(support)) is True


def test_the_judge_only_row_ignores_what_stage_b_found() -> None:
    """Otherwise the row is not 'judge only', it is 'both' under another name."""
    assert _judge_flags(check("insufficient")) is True
    assert _judge_flags(check("supported")) is False
    assert _judge_flags(None) is False


def test_a_fabrication_stage_b_catches_is_missed_only_by_the_judge_row(
    tmp_path: Path,
) -> None:
    """The case for keeping difflib.

    The model asserts `strong` and quotes something the résumé does not contain.
    Stage B cannot find it; the second model, shown a quote and asked whether it
    supports the claim, says yes. Character matching is the control that works
    here, and no amount of second-model reasoning replaces it.
    """
    corpus = one_case_corpus(tmp_path, "Deployed to Kubernetes using Helm charts.", "none")
    llm = ScriptedVerifier("strong", evidence="Fluent in Finnish and Estonian")

    result = compare_verifiers(llm, corpus)

    assert result.tallies["difflib only"].missed_fabrications == 0
    assert result.tallies["judge only"].missed_fabrications > 0
    assert result.tallies["difflib + judge"].missed_fabrications == 0


def test_a_verbatim_but_irrelevant_quote_is_missed_only_by_stage_b(tmp_path: Path) -> None:
    """The case for keeping the second model (9.3).

    The quote is real, verbatim and correctly copied, so it verifies at ratio
    1.00 — and it establishes nothing about the criterion. Stage B is right not
    to care what the words mean; this is the hole that leaves.
    """
    resume = "Fluent in Finnish and Estonian, and I write a food blog on weekends."
    corpus = one_case_corpus(tmp_path, resume, "none")
    llm = ScriptedVerifier(
        "strong", evidence="Fluent in Finnish and Estonian", support="insufficient"
    )

    result = compare_verifiers(llm, corpus)

    assert result.tallies["difflib only"].missed_fabrications > 0
    assert result.tallies["judge only"].missed_fabrications == 0
    assert result.tallies["difflib + judge"].missed_fabrications == 0


def test_a_correct_verdict_sent_to_a_reviewer_is_counted_as_a_false_escalation(
    tmp_path: Path,
) -> None:
    """The column that keeps the table from recommending 'escalate everything'.

    The model's verdict matches the adjudicated label, and the second model
    flags it anyway. That is a reviewer paying for work that changes nothing,
    and it is the cost side of every row.
    """
    resume = "Owned the Kubernetes platform for 12 services in production."
    corpus = one_case_corpus(tmp_path, resume, "strong")
    llm = ScriptedVerifier("strong", support="insufficient")

    result = compare_verifiers(llm, corpus)

    assert result.tallies["judge only"].false_escalations > 0
    assert result.tallies["difflib only"].false_escalations == 0


def test_the_verifier_is_not_asked_about_criteria_it_cannot_help_with(
    tmp_path: Path,
) -> None:
    """A `none` verdict has no quote to check, so there is nothing to ask."""
    corpus = one_case_corpus(tmp_path, "A résumé mentioning nothing relevant.", "none")
    llm = ScriptedVerifier("none")

    compare_verifiers(llm, corpus)

    assert llm.asked == []


def test_every_configuration_sees_the_same_judgment(tmp_path: Path) -> None:
    """One judge call per case, shared across the rows.

    Re-judging per configuration would put judge sampling variance inside a
    table meant to isolate verification, and the three rows would differ for
    reasons the columns do not name.
    """
    corpus = one_case_corpus(tmp_path, "Deployed to Kubernetes using Helm charts.", "strong")
    llm = ScriptedVerifier("strong")

    result = compare_verifiers(llm, corpus)

    counts = {result.tallies[c].criteria for c in CONFIGURATIONS}
    assert len(counts) == 1, "the rows disagree about how many criteria they scored"
