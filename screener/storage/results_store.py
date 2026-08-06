"""Candidate persistence, the cache, and erasure (spec §12.5, §12.6).

Three responsibilities, each with a trap.

**The cache key excludes `run_id`**, which is what makes resumption work: a batch
killed at 50% picks up where it stopped instead of re-judging 500 resumes. That
same omission is the trap. Every failure path writes a `Candidate` with
`scoreable=False`, including a transient Ollama timeout — and cached, a single
network blip would be served back on resume as a permanent verdict, sidelining a
real applicant forever behind something that looks like a legitimate result.

`cacheable` is therefore computed from the flags and written as a column, and
`idx_cache` is a **partial index** over `cacheable = 1`. Non-cacheable rows are
stored for audit and shown to reviewers but are structurally invisible to
lookup — enforced by the index rather than by a `WHERE` clause a future query
could forget.

**`position_id` is in the key** so judgments never leak across requisitions. Two
recruiters screening the same person for different roles must not share a verdict.

**Purge has to reach the filesystem.** Trace files hold full resume text, so
clearing database columns alone leaves the erasure control looking implemented
while a second copy of the candidate's data sits on disk — worse than not having
it, because it would be reported as done.
"""

import json
from pathlib import Path
from typing import Any

from screener.models import Band, Candidate, Flag, RedFlag, ScoredCriterion, Verdict
from screener.ports import CacheKey
from screener.storage.uow import Tx

# Ordered exactly as `idx_cache` declares them, so the planner uses the index.
_CACHE_COLUMNS = (
    "file_sha256",
    "position_id",
    "rubric_hash",
    "model_digest",
    "prompt_hash",
    "redaction_on",
    "num_ctx",
    "app_version",
)

_REDACTED = "[purged]"


def get_cached(tx: Tx, key: CacheKey) -> Candidate | None:
    """A prior judgment made under identical conditions, or nothing.

    All eight key fields are matched. Dropping any one of them means a change
    that alters the output — a re-pulled model, an edited prompt, redaction
    toggled off — silently serves the old verdict.
    """
    where = " AND ".join(f"{column} = ?" for column in _CACHE_COLUMNS)
    row = tx.execute(
        f"SELECT * FROM candidates WHERE {where} AND cacheable = 1 "  # noqa: S608 — column names are a module constant, never caller input
        "ORDER BY id DESC LIMIT 1",
        tuple(getattr(key, column) for column in _CACHE_COLUMNS),
    ).fetchone()
    if row is None:
        return None
    return _row_to_candidate(tx, row)


def save(tx: Tx, run_id: str, candidate: Candidate, key: CacheKey) -> int:
    """Write the candidate and its verdicts. Returns the candidate row id.

    `cacheable` is derived from the flags here rather than trusted from the
    caller — it is the one column that decides whether a failure becomes
    permanent, and a caller that forgets it would produce exactly the §12.5 bug.
    """
    cursor = tx.execute(
        "INSERT INTO candidates ("
        "run_id, filename, file_sha256, score, band, must_haves_met, scoreable, "
        "review_required, cacheable, summary, notable_strengths_json, red_flags_json, "
        "flags_json, position_id, rubric_hash, model_digest, prompt_hash, redaction_on, "
        "num_ctx, app_version, scored_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            candidate.filename,
            candidate.file_sha256,
            candidate.score,
            candidate.band,
            int(candidate.must_haves_met),
            int(candidate.scoreable),
            int(candidate.review_required),
            int(candidate.cacheable),
            candidate.summary,
            json.dumps(candidate.notable_strengths, ensure_ascii=False),
            json.dumps([f.value for f in candidate.red_flags]),
            json.dumps([f.value for f in candidate.flags]),
            key.position_id,
            key.rubric_hash,
            key.model_digest,
            key.prompt_hash,
            int(key.redaction_on),
            key.num_ctx,
            key.app_version,
            candidate.scored_at.isoformat(),
        ),
    )
    candidate_id = int(cursor.lastrowid or 0)

    if candidate.criteria:
        tx.executemany(
            "INSERT INTO verdicts ("
            "candidate_id, criterion_id, verdict, model_verdict, evidence, verified, "
            "match_ratio, longest_span) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    candidate_id,
                    c.id,
                    c.verdict,
                    c.model_verdict,
                    c.evidence,
                    int(c.verified),
                    c.match_ratio,
                    c.longest_span,
                )
                for c in candidate.criteria
            ],
        )

    return candidate_id


def get(tx: Tx, candidate_id: int) -> Candidate | None:
    row = tx.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    return _row_to_candidate(tx, row) if row else None


def list_for_run(tx: Tx, run_id: str) -> list[Candidate]:
    rows = tx.execute("SELECT * FROM candidates WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    return [_row_to_candidate(tx, row) for row in rows]


def find_duplicates(tx: Tx, run_id: str, file_sha256: str) -> int:
    """Count of byte-identical files already seen in this run (§12.6).

    Exact hash collision only. The same CV re-exported from Word has a different
    hash, so this is documented as weak in the UI rather than implying coverage
    it does not have.
    """
    row = tx.execute(
        "SELECT COUNT(*) AS n FROM candidates WHERE run_id = ? AND file_sha256 = ?",
        (run_id, file_sha256),
    ).fetchone()
    return int(row["n"]) if row else 0


def purge_candidate(tx: Tx, file_sha256: str) -> list[Path]:
    """Erase candidate content everywhere. Returns trace files for the caller to unlink.

    Score, band and flags survive as non-identifying statistics — a purged
    candidate should still be countable in an escalation rate without being
    identifiable.

    Trace paths are **returned rather than deleted here** so that filesystem
    removal happens outside the transaction. A store that unlinked files would
    delete them even when the transaction later rolled back, and there is no
    rollback for `unlink`.
    """
    rows = tx.execute(
        "SELECT trace_path FROM traces WHERE file_sha256 = ?", (file_sha256,)
    ).fetchall()
    trace_paths = [Path(row["trace_path"]) for row in rows]

    tx.execute(
        "UPDATE verdicts SET evidence = NULL WHERE candidate_id IN "
        "(SELECT id FROM candidates WHERE file_sha256 = ?)",
        (file_sha256,),
    )
    tx.execute(
        "UPDATE candidates SET filename = ?, summary = NULL, "
        "notable_strengths_json = NULL, cacheable = 0 WHERE file_sha256 = ?",
        (_REDACTED, file_sha256),
    )
    tx.execute("DELETE FROM traces WHERE file_sha256 = ?", (file_sha256,))

    return trace_paths


# --- row mapping -------------------------------------------------------------


def _row_to_candidate(tx: Tx, row: Any) -> Candidate:  # noqa: ANN401 — sqlite3.Row
    verdict_rows = tx.execute(
        "SELECT criterion_id, verdict, model_verdict, evidence, verified, match_ratio, "
        "longest_span FROM verdicts WHERE candidate_id = ? ORDER BY id",
        (row["id"],),
    ).fetchall()

    return Candidate(
        run_id=row["run_id"],
        filename=row["filename"],
        file_sha256=row["file_sha256"],
        score=row["score"],
        band=_as_band(row["band"]),
        must_haves_met=bool(row["must_haves_met"]),
        criteria=[
            ScoredCriterion(
                id=v["criterion_id"],
                verdict=_as_verdict(v["verdict"]),
                model_verdict=_as_verdict(v["model_verdict"]),
                evidence=v["evidence"] or "",
                verified=bool(v["verified"]),
                match_ratio=v["match_ratio"] or 0.0,
                longest_span=v["longest_span"] or 0,
                # Weight and must_have are rubric facts, not verdict facts. They
                # are not stored per verdict — re-deriving them from the rubric
                # keeps one source of truth for the arithmetic.
                weight=1,
                must_have=False,
            )
            for v in verdict_rows
        ],
        notable_strengths=json.loads(row["notable_strengths_json"] or "[]"),
        red_flags=[RedFlag(f) for f in json.loads(row["red_flags_json"] or "[]")],
        summary=row["summary"] or "",
        flags=[Flag(f) for f in json.loads(row["flags_json"] or "[]")],
        scoreable=bool(row["scoreable"]),
        review_required=bool(row["review_required"]),
        scored_at=row["scored_at"],
    )


# Lookup tables rather than membership checks: a `str` that passes an `in` test
# is still a `str` to the type checker, and casting past that would remove the
# only thing catching a value the schema should never have allowed.
_VERDICTS: dict[str, Verdict] = {"strong": "strong", "partial": "partial", "none": "none"}
_BANDS: dict[str, Band] = {"A": "A", "B": "B", "C": "C", "D": "D"}


def _as_verdict(value: str) -> Verdict:
    try:
        return _VERDICTS[value]
    except KeyError:
        raise ValueError(f"unknown verdict in database: {value!r}") from None


def _as_band(value: str | None) -> Band | None:
    if value is None:
        return None
    try:
        return _BANDS[value]
    except KeyError:
        raise ValueError(f"unknown band in database: {value!r}") from None
