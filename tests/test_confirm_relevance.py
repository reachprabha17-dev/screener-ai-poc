"""A second, narrower opinion on what verify_evidence's crude relevance check
flagged (spec 10.5 C). What the answer *does* to a criterion is
tests/test_reconcile_relevance.py, which is pure and needs no client at all.
"""

from typing import Any

from conftest import make_rubric

from screener.core.verify_evidence import align
from screener.llm.confirm_relevance import build_user_message, confirm_relevance, targets
from screener.models import ScoredCriterion

RESUME = (
    "Asha Nair. Senior Backend Engineer, 2019-2024. Led the migration of a payments "
    "monolith to microservices in Go. Managed EKS clusters for 40 services."
)

RUBRIC = make_rubric(
    ("C1", True, 3, "Has built production backend services for at least five years"),
    ("C2", False, 2, "Kubernetes in production"),
    ("C3", False, 1, "Fintech background"),
    ("C4", False, 1, "Payments experience"),
)


class FakeLLM:
    def __init__(self, reply: dict[str, Any] | None = None) -> None:
        self.reply = reply or {"checks": []}
        self.calls: list[tuple[str, str, str]] = []

    def chat_json(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((model, system, user))
        return self.reply


def criterion(
    criterion_id: str, evidence: str, *, evidence_irrelevant: bool = True
) -> ScoredCriterion:
    result = align(evidence, RESUME)
    return ScoredCriterion(
        id=criterion_id,
        verdict="strong",
        model_verdict="strong",
        evidence=evidence,
        verified=True,
        match_ratio=result.ratio,
        longest_span=result.longest_span,
        match_blocks=result.blocks,
        evidence_irrelevant=evidence_irrelevant,
        weight=1,
        must_have=False,
    )


def test_targets_only_the_criteria_flagged_irrelevant() -> None:
    flagged = criterion("C1", "Led the migration of a payments monolith to microservices in Go")
    clean = criterion("C2", "Managed EKS clusters for 40 services", evidence_irrelevant=False)

    assert [c.id for c in targets([flagged, clean])] == ["C1"]


def test_no_call_is_made_when_nothing_was_flagged() -> None:
    llm = FakeLLM()

    assert confirm_relevance(llm, [], RUBRIC, RESUME) == []
    assert llm.calls == []


def test_one_call_covers_every_flagged_criterion() -> None:
    llm = FakeLLM({"checks": [{"id": "C1", "related": True, "rationale": "same subject"}]})
    flagged = [criterion("C1", "Led the migration of a payments monolith to microservices in Go")]

    checks = confirm_relevance(llm, flagged, RUBRIC, RESUME)

    assert len(llm.calls) == 1
    assert checks[0].id == "C1"
    assert checks[0].related is True


def test_the_requirement_and_context_are_both_in_the_message() -> None:
    llm = FakeLLM()
    flagged = [criterion("C1", "Led the migration of a payments monolith to microservices in Go")]

    confirm_relevance(llm, flagged, RUBRIC, RESUME)

    _, _, user = llm.calls[0]
    assert "Has built production backend services for at least five years" in user
    assert "Led the migration of a payments monolith to microservices in Go" in user


def test_checks_for_unknown_criteria_are_dropped() -> None:
    llm = FakeLLM(
        {
            "checks": [
                {"id": "C1", "related": True, "rationale": ""},
                {"id": "C99", "related": True, "rationale": ""},
            ]
        }
    )
    flagged = [criterion("C1", "Led the migration of a payments monolith to microservices in Go")]

    checks = confirm_relevance(llm, flagged, RUBRIC, RESUME)

    assert [c.id for c in checks] == ["C1"]


def test_the_user_message_matches_build_user_message() -> None:
    flagged = [criterion("C1", "Led the migration of a payments monolith to microservices in Go")]
    llm = FakeLLM()

    confirm_relevance(llm, flagged, RUBRIC, RESUME)

    _, _, user = llm.calls[0]
    assert user == build_user_message(flagged, RUBRIC, RESUME)
