"""Shared storage fixtures — the foreign-key chain most tests need before anything else."""

from screener.models import Criterion, Rubric
from screener.storage import rubrics_store
from screener.storage.uow import Tx


def make_rubric(
    position_id: str = "p1",
    rubric_id: str = "r1",
    version: int = 1,
    created_by: str = "poc-operator",
) -> Rubric:
    return Rubric(
        id=rubric_id,
        position_id=position_id,
        version=version,
        created_by=created_by,
        criteria=[
            Criterion(id="C1", text="5+ years backend", must_have=True, weight=3),
            Criterion(id="C2", text="Kubernetes", weight=2),
            Criterion(id="C3", text="Go", weight=1),
            Criterion(id="C4", text="Mentoring", weight=1),
        ],
    )


def make_rubric_row(tx: Tx, position_id: str = "p1", rubric_id: str = "r1") -> Rubric:
    rubric = make_rubric(position_id=position_id, rubric_id=rubric_id)
    rubrics_store.create(tx, rubric)
    return rubric
