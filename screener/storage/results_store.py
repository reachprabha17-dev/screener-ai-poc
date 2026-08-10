"""Candidate persistence, the cache, and erasure (spec 12.5, 12.6).

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

import hashlib
import json
from datetime import datetime
from typing import Any

from screener.models import (
    Band,
    Candidate,
    Decision,
    EscalationReason,
    Flag,
    RedFlag,
    ScoredCriterion,
    Span,
    Support,
    Verdict,
)
from screener.models import MatchBlock as MatchBlockModel
from screener.ports import CacheKey
from screener.storage.uow import Tx

# Ordered exactly as `idx_cache` declares them, so the planner uses the index.
_CACHE_COLUMNS = (
    "file_sha256",
    "position_id",
    "rubric_hash",
    "judge_digest",
    "verifier_digest",
    "prompt_hash",
    "redaction_on",
    "num_ctx",
    "app_version",
)

_REDACTED = "[purged]"


def get_cached(tx: Tx, key: CacheKey) -> Candidate | None:
    """A prior judgment made under identical conditions, or nothing.

    All nine key fields are matched. Dropping any one of them means a change
    that alters the output — a re-pulled model, an edited prompt, redaction
    toggled off, a different verifier — silently serves the old verdict.
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
    permanent, and a caller that forgets it would produce exactly the 12.5 bug.
    """
    cursor = tx.execute(
        "INSERT INTO candidates ("
        "run_id, filename, file_sha256, resume_text, sent_text, sent_text_sha256, "
        "redaction_map_json, score, band, must_haves_met, scoreable, "
        "review_required, cacheable, escalation_reasons_json, verification_status, "
        "summary, notable_strengths_json, red_flags_json, "
        "flags_json, position_id, rubric_hash, judge_digest, verifier_digest, "
        "prompt_hash, redaction_on, num_ctx, app_version, scored_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?)",
        (
            run_id,
            candidate.filename,
            candidate.file_sha256,
            candidate.resume_text or None,
            candidate.sent_text or None,
            _sha256(candidate.sent_text),
            _dump_spans(candidate.redaction_map),
            candidate.score,
            candidate.band,
            int(candidate.must_haves_met),
            int(candidate.scoreable),
            int(candidate.review_required),
            int(candidate.cacheable),
            json.dumps([r.value for r in candidate.escalation_reasons]),
            candidate.verification_status,
            candidate.summary,
            json.dumps(candidate.notable_strengths, ensure_ascii=False),
            json.dumps([f.value for f in candidate.red_flags]),
            json.dumps([f.value for f in candidate.flags]),
            key.position_id,
            key.rubric_hash,
            key.judge_digest,
            key.verifier_digest,
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
            "match_ratio, longest_span, match_blocks_json, negation_suspected"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    _dump_blocks(c.match_blocks),
                    int(c.negation_suspected),
                )
                for c in candidate.criteria
            ],
        )

    return candidate_id


def save_verification(tx: Tx, candidate_id: int, candidate: Candidate) -> None:
    """Write what phase 2 produced. **Never score, band, or verdict.**

    The column list is the enforcement of 1.9 at the storage layer: the verifier
    escalates and proposes, so the only things this statement can change are
    flags, escalation reasons, review state and the per-criterion verifier
    columns. `reconcile_judge` already refuses to touch a verdict; writing the
    verdict column here anyway would make that guarantee depend on a pure
    function nobody re-checks rather than on the SQL that actually runs.
    """
    tx.execute(
        "UPDATE candidates SET verification_status = ?, review_required = ?, "
        "escalation_reasons_json = ?, flags_json = ?, verifier_digest = ? WHERE id = ?",
        (
            candidate.verification_status,
            int(candidate.review_required),
            json.dumps([r.value for r in candidate.escalation_reasons]),
            json.dumps([f.value for f in candidate.flags]),
            _verifier_digest_of(tx, candidate_id),
            candidate_id,
        ),
    )
    tx.executemany(
        "UPDATE verdicts SET support = ?, suggested_verdict = ?, verifier_rationale = ?, "
        "absence_confirmed = ?, absence_evidence = ?, negation_suspected = ? "
        "WHERE candidate_id = ? AND criterion_id = ?",
        [
            (
                c.support,
                c.suggested_verdict,
                c.verifier_rationale,
                None if c.absence_confirmed is None else int(c.absence_confirmed),
                c.absence_evidence or None,
                int(c.negation_suspected),
                candidate_id,
                c.id,
            )
            for c in candidate.criteria
        ],
    )


def save_decision(
    tx: Tx, candidate_id: int, decision: Decision, actor_id: str, at: datetime
) -> None:
    """Write the current decision. History goes to `overrides` (12.8).

    Two places, deliberately. `candidates.decision` answers "where does this
    person stand" in one index probe, which is what every list screen and the
    sign-off precondition ask; `overrides` answers "who changed it, from what,
    and why". Deriving the former from the latter would make the common query a
    correlated subquery over an ever-growing history, and deriving the latter
    from the former is impossible — an in-place update keeps no past.
    """
    tx.execute(
        "UPDATE candidates SET decision = ?, decided_by = ?, decided_at = ? WHERE id = ?",
        (decision, actor_id, at.isoformat(), candidate_id),
    )


def mark_unverified_as_skipped(tx: Tx, run_id: str) -> int:
    """Close out candidates that no phase-2 job will ever reach.

    Called when a run goes to `done` without a verify phase — verification is
    off for the run, or nothing in it was scoreable. Left alone, those rows keep
    `verification_status='pending'` forever, and `pending` means "not verified
    *yet*": every consumer that treats it as outstanding — the provisional badge,
    the bulk exclusion, the sign-off precondition — would block on work that is
    never going to happen.

    `skipped` is the honest value. It says verification did not run, which is
    exactly true, and is distinguishable from `done` for anyone auditing later.
    """
    cursor = tx.execute(
        "UPDATE candidates SET verification_status = 'skipped' "
        "WHERE run_id = ? AND verification_status = 'pending'",
        (run_id,),
    )
    return int(cursor.rowcount or 0)


def _verifier_digest_of(tx: Tx, candidate_id: int) -> str | None:
    """The digest recorded on the run, copied onto the candidate at verify time.

    Denormalized for the same reason the other cache columns are: a cache lookup
    should be one index probe, not a join back to `runs`.
    """
    row = tx.execute(
        "SELECT r.verifier_digest AS digest FROM candidates c "
        "JOIN runs r ON r.id = c.run_id WHERE c.id = ?",
        (candidate_id,),
    ).fetchone()
    return row["digest"] if row else None


def get(tx: Tx, candidate_id: int) -> Candidate | None:
    row = tx.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    return _row_to_candidate(tx, row) if row else None


def list_for_run(tx: Tx, run_id: str) -> list[Candidate]:
    rows = tx.execute("SELECT * FROM candidates WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    return [_row_to_candidate(tx, row) for row in rows]


def find_duplicates(tx: Tx, run_id: str, file_sha256: str) -> int:
    """Count of byte-identical files already seen in this run (12.6).

    Exact hash collision only. The same CV re-exported from Word has a different
    hash, so this is documented as weak in the UI rather than implying coverage
    it does not have.
    """
    row = tx.execute(
        "SELECT COUNT(*) AS n FROM candidates WHERE run_id = ? AND file_sha256 = ?",
        (run_id, file_sha256),
    ).fetchone()
    return int(row["n"]) if row else 0


def purge_candidate(tx: Tx, file_sha256: str) -> None:
    """Erase candidate content everywhere in the database (12.10).

    Score, band, flags and decision survive as non-identifying statistics — a
    purged candidate should still be countable in an escalation rate without
    being identifiable.

    **Every text column has to be listed here.** v6 added four of them
    (`resume_text`, `sent_text`, `redaction_map_json`, `verdicts.absence_evidence`)
    and `candidates` stopped being a results table the moment it started holding
    full résumés. Missing one leaves erasure looking implemented while a complete
    copy of the person's CV sits in the row next to the redacted one — which is
    worse than not having the control, because it gets reported as done.

    Files are the caller's job (`service.purge_candidate`): there is no rollback
    for `unlink`, so a store that deleted them would destroy data a later abort
    was supposed to keep.
    """
    tx.execute(
        "UPDATE verdicts SET evidence = NULL, absence_evidence = NULL "
        "WHERE candidate_id IN (SELECT id FROM candidates WHERE file_sha256 = ?)",
        (file_sha256,),
    )
    tx.execute(
        "UPDATE candidates SET filename = ?, summary = NULL, "
        "notable_strengths_json = NULL, resume_text = NULL, sent_text = NULL, "
        "sent_text_sha256 = NULL, redaction_map_json = NULL, cacheable = 0 "
        "WHERE file_sha256 = ?",
        (_REDACTED, file_sha256),
    )


# --- row mapping -------------------------------------------------------------


def _sha256(text: str) -> str | None:
    """Identity of what the model actually read.

    Stored beside `sent_text` so a later question — "was this candidate judged
    against the same string we are now showing?" — is answerable without
    diffing two large columns.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def _dump_spans(spans: list[Span]) -> str | None:
    return json.dumps([s.model_dump() for s in spans]) if spans else None


def _dump_blocks(blocks: list[MatchBlockModel]) -> str | None:
    return json.dumps([b.model_dump() for b in blocks]) if blocks else None


def _row_to_candidate(tx: Tx, row: Any) -> Candidate:  # noqa: ANN401 — sqlite3.Row
    verdict_rows = tx.execute(
        "SELECT criterion_id, verdict, model_verdict, evidence, verified, match_ratio, "
        "longest_span, match_blocks_json, negation_suspected, support, suggested_verdict, "
        "verifier_rationale, absence_confirmed, absence_evidence "
        "FROM verdicts WHERE candidate_id = ? ORDER BY id",
        (row["id"],),
    ).fetchall()

    return Candidate(
        id=row["id"],
        run_id=row["run_id"],
        filename=row["filename"],
        file_sha256=row["file_sha256"],
        resume_text=row["resume_text"] or "",
        sent_text=row["sent_text"] or "",
        redaction_map=[Span(**s) for s in json.loads(row["redaction_map_json"] or "[]")],
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
                match_blocks=[
                    MatchBlockModel(**b) for b in json.loads(v["match_blocks_json"] or "[]")
                ],
                negation_suspected=bool(v["negation_suspected"]),
                support=_as_support(v["support"]),
                suggested_verdict=(
                    _as_verdict(v["suggested_verdict"]) if v["suggested_verdict"] else None
                ),
                verifier_rationale=v["verifier_rationale"] or "",
                absence_confirmed=(
                    None if v["absence_confirmed"] is None else bool(v["absence_confirmed"])
                ),
                absence_evidence=v["absence_evidence"] or "",
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
        escalation_reasons=[
            EscalationReason(r) for r in json.loads(row["escalation_reasons_json"] or "[]")
        ],
        verification_status=row["verification_status"],
        decision=row["decision"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        scored_at=row["scored_at"],
    )


# Lookup tables rather than membership checks: a `str` that passes an `in` test
# is still a `str` to the type checker, and casting past that would remove the
# only thing catching a value the schema should never have allowed.
_VERDICTS: dict[str, Verdict] = {"strong": "strong", "partial": "partial", "none": "none"}
_BANDS: dict[str, Band] = {"A": "A", "B": "B", "C": "C", "D": "D"}
_SUPPORTS: dict[str, Support] = {
    "supported": "supported",
    "insufficient": "insufficient",
    "contradicted": "contradicted",
}


def _as_support(value: str | None) -> Support | None:
    if value is None:
        return None
    try:
        return _SUPPORTS[value]
    except KeyError:
        raise ValueError(f"unknown support value in database: {value!r}") from None


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
