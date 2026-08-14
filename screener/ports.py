"""Protocols for everything that touches the outside world (spec 6).

Infrastructure is named here and implemented in ``clients/``, ``intake/`` and
``storage/``. Callers depend on these Protocols, never on a concrete class, so
swapping Ollama for vLLM, xberg for another parser, or SQLite for Postgres means
one new file satisfying a Protocol plus a config value.

**This is the portability mechanism**, and it is why no ORM is added for the same
purpose (22.2) — adding an abstraction to achieve what an existing abstraction
already achieves is duplication.

Like ``models``, this module imports nothing else from the package.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from screener.models import Candidate, ParseResult


class CacheKey(BaseModel):
    """Everything that changes the output must be in the key.

    ``position_id`` is included so results never leak across requisitions: two
    recruiters screening the same person for different roles must not share a
    judgment. ``run_id`` is deliberately absent — that is what makes resumption
    work.
    """

    model_config = ConfigDict(frozen=True)

    file_sha256: str
    position_id: str
    rubric_hash: str
    judge_digest: str
    # Verification is part of the stored result from v6 on, so a changed
    # verifier has to invalidate the entry. Empty string when verification is
    # disabled — a nullable key field would make two different configurations
    # hash to the same lookup.
    verifier_digest: str = ""
    prompt_hash: str
    redaction_on: bool
    num_ctx: int
    app_version: str


class Job(BaseModel):
    """One file to screen, claimed by exactly one worker at a time."""

    model_config = ConfigDict(extra="forbid")

    id: int
    run_id: str
    # Which pass this job belongs to (17.4). Part of the job's identity, not a
    # property of it: the same resume has one judge job and one verify job, and
    # they are distinct rows.
    phase: Literal["judge", "verify"] = "judge"
    file_path: Path
    file_sha256: str | None = None
    # Set on verify jobs only — phase 2 works from a stored candidate, not a file.
    candidate_id: int | None = None
    attempts: int = 0
    claimed_by: str | None = None
    claimed_at: datetime | None = None


@runtime_checkable
class LLMClient(Protocol):
    """Chat models that can be constrained to a JSON schema.

    **Every method names its model.** v6 runs two — a judge and a deliberately
    different verifier — and a client with an implicit default would make it
    impossible to tell from a call site which weights produced a result, which is
    the one thing the provenance record exists to answer.
    """

    def chat_json(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one completion constrained to ``schema`` and return parsed JSON."""
        ...

    def count_tokens(self, model: str, text: str) -> int:
        """Exact prompt token count for a bare string."""
        ...

    def count_prompt_tokens(self, model: str, system: str, user: str) -> int:
        """Exact size of the assembled two-message prompt, for the 10.1 pre-check.

        Separate from ``count_tokens`` because the chat template adds framing the
        raw strings do not carry, and under-counting is the direction that lets a
        prompt pass the budget check and then overflow ``num_ctx`` silently.
        """
        ...

    def health(self) -> bool: ...

    def digest(self, model: str) -> str:
        """Digest of a model's weights — recorded per decision for provenance."""
        ...

    def ensure_loaded(self, model: str) -> None:
        """Make this model resident, unloading the other (17.4).

        Called once per phase. 12 GB of VRAM does not hold both, and swapping per
        resume costs a 10–20 s load on every candidate.
        """
        ...

    def unload(self, model: str) -> None: ...


@runtime_checkable
class ResumeParser(Protocol):
    """Turns a resume file into text. Implementations run sandboxed (8.4).

    Returns a ``ParseResult`` rather than raising: a file that cannot be read is
    an expected outcome of a run, and the candidate still has to reach a human.
    """

    def parse(self, path: Path) -> ParseResult: ...


@runtime_checkable
class Tx(Protocol):
    """An open transaction handed to stores. Stores never open one themselves.

    The cursor type is deliberately implementation-defined: naming ``sqlite3``
    here would couple the port to the adapter it exists to abstract away.
    """

    # noqa ANN401: the bound parameters and the returned cursor are exactly what
    # the backing driver defines — that is the point of the seam.
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any: ...  # noqa: ANN401


@runtime_checkable
class UnitOfWork(Protocol):
    """Transaction boundary, owned by the service layer.

    Without a single boundary, ``overrides`` and ``audit_log`` can diverge and
    you eventually hold an override with no audit trail. Never held open across
    an LLM call — the worker's pattern is claim (tx) → screen (no tx) → save (tx).
    """

    def __enter__(self) -> Tx: ...
    def __exit__(self, *exc: object) -> None: ...


@runtime_checkable
class ResultsStore(Protocol):
    def get_cached(self, tx: Tx, key: CacheKey) -> Candidate | None:
        """Return a prior judgment made under identical conditions.

        Non-cacheable rows (transient failures) are structurally invisible here
        via a partial index, not filtered in Python (12.5).
        """
        ...

    def save(self, tx: Tx, run_id: str, candidate: Candidate) -> None: ...

    def purge_candidate(self, tx: Tx, file_sha256: str) -> None:
        """Erase candidate content across every store, including trace files.

        The trace files are the step that is easy to forget and the one that
        would make the whole control ineffective (12.6, 17).
        """
        ...


@runtime_checkable
class JobQueue(Protocol):
    def snapshot_folder(self, tx: Tx, run_id: str, folder: Path) -> int:
        """Freeze the folder into jobs. A run is a defined set at a point in time."""
        ...

    def claim_next(self, tx: Tx, worker_id: str) -> Job | None: ...
    def heartbeat(self, tx: Tx, job_id: int, worker_id: str) -> None: ...
    def complete(self, tx: Tx, job_id: int) -> None: ...
    def fail(self, tx: Tx, job_id: int, error: str, retryable: bool) -> None: ...

    def reclaim_orphaned(self, tx: Tx, worker_id: str) -> int:
        """Return jobs this worker claimed in a previous life to ``pending``.

        Clock-free by construction: an air-gapped box has no NTP, and absolute
        lease expiry reclaims live work or strands dead work on a clock step
        (16.5).
        """
        ...
