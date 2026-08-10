"""Live model behaviour (spec 9, build gate 20 step 9).

The named gate is **exact-verdict-set compliance ≥95% pre-retry** — the share of
first attempts that return precisely the rubric's ids, each once. It is a
property of the model and the prompt together, so it can only be measured
against the real thing.

Why it is a gate rather than a nice-to-have: the corrective retry costs a second
full inference pass. At 4.7 s/resume a 90% compliance rate adds ~8 minutes to a
1,000-CV run, and every non-compliant *second* attempt escalates a real candidate
to a human for a reason that has nothing to do with them.

Run with `pytest -m live`.
"""

import re

import pytest

from screener.clients.ollama_client import OllamaClient
from screener.core.validate_verdicts import validate_verdicts
from screener.llm.extract_rubric import extract_rubric
from screener.llm.judge_resume import build_user_message, judge_resume
from screener.models import Criterion, JudgeOutput, Rubric
from screener.ports import LLMClient

pytestmark = pytest.mark.live


def rubric_from(*specs: tuple[str, bool, int]) -> Rubric:
    """Build a rubric with **real criterion text**.

    `conftest.make_rubric` fills in placeholders like "criterion C1", which is
    fine for the pure functions but wrong here: a model asked to judge a resume
    against meaningless criteria produces meaningless verdicts, and any
    measurement taken from them says nothing about the prompt.
    """
    return Rubric(
        id="r-live",
        position_id="p-live",
        version=1,
        created_by="tester",
        criteria=[
            Criterion(id=f"C{i}", text=text, must_have=must, weight=weight)
            for i, (text, must, weight) in enumerate(specs, start=1)
        ],
    )


# Eight criteria: enough that omitting one is a realistic failure, and enough to
# exercise the id-set check with room to get it wrong. Deliberately mixed —
# some plainly met, some plainly absent, some genuinely borderline — so a model
# that answered `strong` to everything would fail rather than score well.
RUBRIC = rubric_from(
    ("5+ years building production backend services", True, 5),
    ("Operating services on Kubernetes in production", True, 4),
    ("Strong Python", False, 3),
    ("Go in production", False, 3),
    ("Relational database schema design and query optimisation", False, 2),
    ("Mentoring or leading other engineers", False, 2),
    ("Rust in production", False, 1),
    ("Machine learning model deployment", False, 1),
)

RESUME = (
    "Asha Nair\n"
    "Senior Backend Engineer\n\n"
    "EXPERIENCE\n"
    "2019-2024, Acme Payments - Staff Engineer. Led the migration of a payments "
    "monolith to microservices in Go, handling 40 million requests per day. "
    "Ran the on-call rotation and owned the Kubernetes platform for 12 services.\n"
    "2016-2019, Initech - Backend Engineer. Built REST APIs in Python and Django. "
    "PostgreSQL schema design and query optimisation.\n\n"
    "SKILLS\n"
    "Python, Go, PostgreSQL, Redis, Kubernetes, Terraform, gRPC.\n"
    "Some exposure to Rust through a training course.\n"
)

COMPLIANCE_SAMPLES = 20
COMPLIANCE_TARGET = 0.95


@pytest.fixture(scope="module")
def llm() -> LLMClient:
    client = OllamaClient()
    if not client.health():
        pytest.skip("ollama unreachable")
    return client


# --- the gate ----------------------------------------------------------------


def test_verdict_set_compliance_meets_the_budget(llm: LLMClient) -> None:
    """The 20 step 9 gate, measured rather than assumed.

    Compliance is checked on the *first* attempt only. The retry exists as a
    safety net, not as the normal path — if it is load-bearing, the prompt is
    wrong and the cost lands on every run.
    """
    schema = JudgeOutput.model_json_schema()
    from screener.llm import load_prompt

    system = load_prompt("judge_resume")
    user = build_user_message(RESUME, RUBRIC)

    compliant = 0
    failures: list[str] = []
    for _ in range(COMPLIANCE_SAMPLES):
        output = JudgeOutput.model_validate(llm.chat_json(system, user, schema))
        check = validate_verdicts(output, RUBRIC)
        if check.ok:
            compliant += 1
        else:
            failures.append(check.describe())

    rate = compliant / COMPLIANCE_SAMPLES
    assert rate >= COMPLIANCE_TARGET, (
        f"pre-retry compliance {rate:.0%} < {COMPLIANCE_TARGET:.0%}; failures: {failures}"
    )


# --- paraphrase invariance ---------------------------------------------------

# The same person, five ways. A screener that gives these different verdicts is
# scoring CV *writing*, not the work described in it.
EQUIVALENT_CVS = {
    "explicit_years": (
        "Asha Nair. Senior Backend Engineer, 7 years. Led the migration of a payments "
        "monolith to microservices in Go, handling 40 million requests per day. Owned the "
        "Kubernetes platform for 12 services and ran the on-call rotation. Built REST APIs "
        "in Python and Django. Mentored three junior engineers."
    ),
    "date_range": (
        "Asha Nair. Senior Backend Engineer. 2019-2026: led the migration of a payments "
        "monolith to microservices in Go, handling 40 million requests per day; owned the "
        "Kubernetes platform for 12 services and ran on-call. 2017-2019: built REST APIs in "
        "Python and Django. Mentored three junior engineers."
    ),
    "terse": (
        "Asha Nair, Senior Backend Engineer (7 yrs). Payments monolith -> Go microservices, "
        "40M req/day. K8s platform owner, 12 services, on-call. Python/Django REST APIs. "
        "Mentored 3 juniors."
    ),
    "verbose": (
        "Asha Nair is a Senior Backend Engineer with seven years of professional experience. "
        "During that time she led the migration of a payments monolith to a microservices "
        "architecture written in Go, a system which handles approximately 40 million requests "
        "each day. She owned the Kubernetes platform supporting 12 services and participated "
        "in the on-call rotation. Earlier work included building REST APIs using Python and "
        "the Django framework. She has also mentored three junior engineers."
    ),
    "reordered": (
        "Asha Nair. Mentored three junior engineers. Built REST APIs in Python and Django. "
        "Owned the Kubernetes platform for 12 services and ran the on-call rotation. Led the "
        "migration of a payments monolith to microservices in Go, handling 40 million "
        "requests per day. Senior Backend Engineer, 7 years."
    ),
}


def test_equivalent_resumes_get_equivalent_verdicts(llm: LLMClient) -> None:
    """Verdicts must follow the facts, not the writing.

    **This test exists because the system failed it.** Measured before the
    9.2 "Judge the facts, not the writing" anchors were added: five equivalent
    CVs produced *three* different verdict tuples, and both must-have criteria
    flipped between `strong` and `partial`. Writing `2019-2026` instead of
    `7 years` was enough to downgrade a must-have.

    That is not a cosmetic problem. 10.4 escalates a `partial` must-have to a
    human, so CV *formatting* — not experience — decided who entered the review
    queue. Date-range formatting tracks CV convention, region and template
    choice rather than capability, which makes it an adverse-impact risk and a
    driver of the escalation budget at the same time.

    Scope, stated precisely: five phrasings of one resume, over criteria the
    resume **does** address. Verdicts on criteria a resume says nothing about
    are *not* stable — see `test_absent_criteria_are_not_phrase_stable` — and no
    prompt wording fixed that. What stops those reaching a score is 10.5(c),
    downstream of this call. Broadening the corpus is step 19's job (18.4).
    """
    supported = rubric_from(
        ("5+ years building production backend services", True, 5),
        ("Operating services on Kubernetes in production", True, 4),
        ("Python in production", False, 3),
        ("Mentoring or leading other engineers", False, 2),
    )

    results = {name: _verdicts(llm, text, supported) for name, text in EQUIVALENT_CVS.items()}
    distinct = set(results.values())

    assert len(distinct) == 1, (
        "equivalent resumes produced different verdicts — the model is scoring "
        f"presentation rather than substance: {results}"
    )


def test_absent_criteria_are_not_phrase_stable(llm: LLMClient) -> None:
    """A limitation, recorded rather than asserted away.

    Asked about a technology the resume never mentions, the model's verdict
    varies with phrasing — and sometimes returns `strong`, quoting a real
    sentence about a *different* technology. Measured: `strong` for "Rust in
    production" evidenced by "...migration ... to microservices in Go".

    No prompt wording has removed this. The control that makes it survivable is
    10.5(c): the quote is real, so alignment passes, but it is not *about* the
    criterion, so the candidate is escalated rather than scored on it.

    This test documents the behaviour and will start failing if the model
    becomes reliable here — which would be worth knowing.
    """
    absent = rubric_from(
        ("Rust in production", False, 1),
        ("Machine learning model deployment", False, 1),
        ("5+ years building production backend services", True, 5),
        ("Operating services on Kubernetes in production", True, 4),
    )

    verdicts = {name: _verdicts(llm, text, absent)[0] for name, text in EQUIVALENT_CVS.items()}

    # Not an aspiration — a record of where the model is unreliable.
    assert "none" in verdicts.values(), verdicts


def test_a_date_range_counts_as_duration(llm: LLMClient) -> None:
    """The specific mechanism behind the failure above, pinned on its own.

    `strong` requires depth, scope *or duration*. The model treated an explicit
    duration as stronger evidence than a computable one, so a date range read as
    weaker than the same span written out.
    """
    duration_rubric = rubric_from(
        ("5+ years building production backend services", True, 5),
        ("Operating services on Kubernetes in production", True, 4),
        ("Strong Python", False, 3),
        ("Mentoring or leading other engineers", False, 2),
    )

    explicit = _verdicts(llm, EQUIVALENT_CVS["explicit_years"], duration_rubric)
    dated = _verdicts(llm, EQUIVALENT_CVS["date_range"], duration_rubric)

    assert explicit[0] == dated[0], (
        f"'7 years' scored {explicit[0]} but '2019-2026' scored {dated[0]} — "
        "the same span, judged differently"
    )


def _verdicts(llm: LLMClient, text: str, rubric: Rubric | None = None) -> tuple[str, ...]:
    result = judge_resume(llm, text, rubric or RUBRIC)
    assert result.output is not None, result.check.describe()
    return tuple(c.verdict for c in sorted(result.output.criteria, key=lambda c: c.id))


# --- schema round-trip -------------------------------------------------------


def test_judge_output_round_trips_through_the_grammar(llm: LLMClient) -> None:
    """`model_json_schema()` → llama.cpp grammar → parsed back into the model.

    The `$defs`/`$ref` that `RedFlag` generates are the part worth exercising: a
    schema the decoder cannot compile fails here, not mid-batch.
    """
    result = judge_resume(llm, RESUME, RUBRIC)

    assert result.ok, result.check.describe()
    assert result.output is not None
    assert {c.id for c in result.output.criteria} == RUBRIC.ids
    assert all(len(c.evidence) <= 300 for c in result.output.criteria)


def test_unsupported_criteria_get_none_and_not_found(llm: LLMClient) -> None:
    """The 10.5(a) consistency gate depends on this exact behaviour.

    The rubric below asks for things the resume plainly does not contain. A
    model that returns `strong` here with honest `not found` evidence is the
    verified attack signature; a model that returns `none` is doing as told.
    """
    absent = rubric_from(
        ("Registered nurse licence", False, 1),
        ("Fluent in Finnish", False, 1),
        ("Commercial pilot rating", False, 1),
        ("Published poetry collection", False, 1),
    )

    result = judge_resume(llm, RESUME, absent)

    assert result.ok, result.check.describe()
    assert result.output is not None
    assert all(c.verdict == "none" for c in result.output.criteria), [
        (c.id, c.verdict, c.evidence) for c in result.output.criteria
    ]


def test_evidence_is_quoted_from_the_resume(llm: LLMClient) -> None:
    """What 10.5(b) verifies. Measured here so a prompt regression is visible.

    Not asserted as exact substring — 1 records that quotes come back
    paraphrased and wrapped, which is why verification is fuzzy alignment rather
    than string matching.
    """
    from screener.core.verify_evidence import align

    result = judge_resume(llm, RESUME, RUBRIC)
    assert result.output is not None

    supported = [c for c in result.output.criteria if c.verdict != "none"]
    assert supported, "expected at least one supported criterion on this resume"

    ratios = [align(c.evidence, RESUME).ratio for c in supported]
    assert sum(r >= 0.6 for r in ratios) >= len(ratios) * 0.8, ratios


# --- rubric extraction -------------------------------------------------------


JD = (
    "Senior Backend Engineer\n\n"
    "Required: 5+ years building production backend services. Strong Python. "
    "Experience operating services on Kubernetes is essential.\n"
    "Preferred: Go, event-driven architectures, mentoring junior engineers.\n"
    "You will own payment-critical systems and participate in on-call.\n"
)


def test_extraction_produces_a_reviewable_rubric(llm: LLMClient) -> None:
    result = extract_rubric(llm, JD)

    assert 4 <= len(result.criteria) <= 12
    assert [c.id for c in result.criteria] == [f"C{i}" for i in range(1, len(result.criteria) + 1)]
    assert all(1 <= c.weight <= 5 for c in result.criteria)
    assert result.must_have_count >= 1  # the JD says "required" and "essential"


def test_extraction_does_not_invent_protected_criteria(llm: LLMClient) -> None:
    """A biased criterion here applies to the whole run, not to one resume.

    And unlike a bad verdict it leaves no trace in any individual result — which
    is why the prompt forbids it and this asserts it.
    """
    result = extract_rubric(llm, JD)
    text = " ".join(c.text for c in result.criteria).casefold()

    # Word boundaries, not substrings: "age" is inside "language" and "manager",
    # both of which are ordinary and correct in a backend rubric.
    for forbidden in ("age", "gender", "nationality", "married", "culture fit", "young"):
        assert re.search(rf"\b{forbidden}\b", text) is None, result.criteria
