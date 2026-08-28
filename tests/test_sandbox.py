"""Parser sandbox (spec 8.4/8.5, build gate 20 step 5).

The gate is "rlimit kill → PARSER_TIMEOUT not a hang". More broadly: every way a
parser can die must return a typed result to a worker that is still running.
A test suite that only feeds this module well-formed input proves nothing about
it, so the child here is hostile by construction (`sandbox_child.py`).

Also asserted: both failure flags are transient. Caching a timeout as a permanent
verdict would sideline a real candidate forever on the strength of one slow
parse — the trap 12.5 exists to close.
"""

import os
import resource
import threading
from pathlib import Path

import pytest

from config.settings import settings
from screener.intake.sandbox import (
    ADDRESS_SPACE_HEADROOM,
    MAX_OPEN_FILES,
    ParseOutcome,
    run_sandboxed,
)
from screener.models import TRANSIENT_FLAGS, Flag

CHILD = "sandbox_child"
TESTS_DIR = str(Path(__file__).parent)


def run(mode: str, *, timeout_s: int = 5) -> object:
    """Invoke the sandbox against a hostile child.

    ``PYTHONPATH`` goes through ``extra_env`` rather than widening the module's
    allowlist, so the production default stays sealed.
    """
    return run_sandboxed(
        Path(mode),
        worker_module=CHILD,
        timeout_s=timeout_s,
        extra_env={"PYTHONPATH": TESTS_DIR},
    )


# --- the contract holds on success ------------------------------------------


def test_well_behaved_child_returns_its_payload() -> None:
    result = run("ok")

    assert result.outcome is ParseOutcome.OK
    assert result.ok is True
    assert result.payload == {"text": "Asha Nair. Senior Backend Engineer.", "page_count": 1}
    assert result.flags == []
    assert result.security_event is False


# --- limits fire, and the parent survives them ------------------------------


def test_cpu_loop_is_killed_and_reported_as_timeout() -> None:
    """The gate: an rlimit kill, not a hang.

    A parser stuck on a malformed structure would otherwise hold the worker —
    and its claimed job — forever.
    """
    result = run("spin", timeout_s=1)

    assert result.outcome is ParseOutcome.TIMEOUT
    assert result.flags == [Flag.PARSER_TIMEOUT]
    assert result.duration_s < 10


def test_idle_child_is_killed_by_the_parents_wall_clock() -> None:
    """A parser blocked on I/O burns no CPU, so RLIMIT_CPU never fires.

    This is why the parent keeps its own timeout rather than trusting the
    limits it set.
    """
    result = run("sleeper", timeout_s=1)

    assert result.outcome is ParseOutcome.TIMEOUT
    assert result.duration_s < 10


def test_memory_bomb_dies_inside_the_child() -> None:
    result = run("memhog", timeout_s=10)

    assert result.outcome in (ParseOutcome.CRASHED, ParseOutcome.TIMEOUT)
    assert result.flags[0] in TRANSIENT_FLAGS


def test_fork_bomb_is_refused() -> None:
    result = run("fork_bomb", timeout_s=5)

    # Either the limit refused the forks and the child reported back, or the
    # child died trying. Both are contained; neither reaches the parent.
    assert result.outcome in (ParseOutcome.OK, ParseOutcome.CRASHED, ParseOutcome.TIMEOUT)
    if result.ok:
        assert result.payload is not None
        assert result.payload["forked"] < 1000


# --- crashes are security events --------------------------------------------


def test_segfault_is_a_security_event() -> None:
    """A parser crash is not a data-quality event.

    It signals a file doing something the parser did not expect, and is reported
    as a security finding even when the likely cause is a corrupt CV. Treating
    crashes as routine noise is how the one signal that an exploit is being
    tried arrives looking like a warning nobody reads.
    """
    result = run("crash")

    assert result.outcome is ParseOutcome.CRASHED
    assert result.flags == [Flag.PARSER_CRASHED]
    assert result.security_event is True
    assert result.killed_by == 11  # SIGSEGV


def test_nonzero_exit_is_a_crash_and_keeps_stderr() -> None:
    result = run("nonzero")

    assert result.outcome is ParseOutcome.CRASHED
    assert result.exit_code == 3
    assert "page 3" in result.stderr


# --- a broken contract is not an empty parse --------------------------------


def test_unparseable_output_is_not_treated_as_an_empty_resume() -> None:
    """Exit 0 with unreadable output means the parent/child contract broke.

    An empty parse is a *cacheable statement about the document*. This is not
    one, and conflating them would store "this CV has no text" as a permanent
    fact about a candidate whose file was never actually read.
    """
    result = run("garbage")

    assert result.outcome is ParseOutcome.INVALID_OUTPUT
    assert result.flags == [Flag.PARSER_CRASHED]
    assert result.security_event is True


def test_json_that_is_not_an_object_is_rejected() -> None:
    assert run("not_an_object").outcome is ParseOutcome.INVALID_OUTPUT


# --- transience --------------------------------------------------------------


@pytest.mark.parametrize("mode", ["spin", "crash", "garbage"])
def test_every_failure_flag_is_transient(mode: str) -> None:
    """None of these may be cached (12.5).

    A network blip or one slow parse must never come back on resume looking like
    a permanent verdict on a candidate.
    """
    result = run(mode, timeout_s=1)

    assert result.flags
    assert set(result.flags) <= TRANSIENT_FLAGS


# --- isolation ---------------------------------------------------------------


def test_host_environment_is_not_inherited() -> None:
    """The parent holds the database path and the Ollama host.

    None of it is useful to a parser, and all of it is readable by one that has
    been taken over.
    """
    marker = "SCREENER_DB_SECRET"
    os.environ[marker] = "should-not-leak"  # noqa: S105 — a canary, not a credential
    try:
        result = run("dump_env")
    finally:
        del os.environ[marker]

    assert result.ok
    assert result.payload is not None
    assert marker not in result.payload["env"]
    assert "OMP_NUM_THREADS" in result.payload["env"]


def test_child_runs_in_a_scratch_directory_that_is_removed() -> None:
    """Anything the parser writes relative to itself lands somewhere we delete."""
    result = run("dump_cwd")

    assert result.ok
    assert result.payload is not None
    cwd = Path(str(result.payload["cwd"]))
    assert "screener-parse-" in cwd.name
    assert not cwd.exists()


def test_child_cwd_is_not_the_repository() -> None:
    result = run("dump_cwd")

    assert result.payload is not None
    assert Path(str(result.payload["cwd"])) != Path.cwd()


# --- the limits themselves, not only their consequences ----------------------


def test_the_declared_rlimits_are_actually_applied_to_the_parser() -> None:
    """The mechanism, asserted directly rather than inferred from an effect.

    Every other test in this file observes a *consequence* — a bomb dies, a loop
    is killed — and every one of them would still pass against a sandbox that
    applied no limits at all: a 64 MB-per-block allocation loop gets killed by
    the host eventually either way. Without this, "the limits are applied"
    was an argument rather than a fact, which is not good enough for the one
    module standing between a hostile document and the host.

    It matters most across a change to *how* they are applied. They are set by
    `rlimit_shim` now and inherited through its `execv`, rather than by a
    `preexec_fn` between fork and exec; this is what shows the migration
    preserved every value.
    """
    result = run("dump_limits", timeout_s=7)

    assert result.ok
    assert result.payload is not None
    limits = result.payload["limits"]

    data = settings.parse_mem_limit_mb * 1024 * 1024
    assert limits["DATA"] == [data, data]
    assert limits["AS"] == [data * ADDRESS_SPACE_HEADROOM, data * ADDRESS_SPACE_HEADROOM]
    # Soft below hard, so a spin takes SIGXCPU rather than a bare SIGKILL.
    assert limits["CPU"] == [7, 8]
    assert limits["FSIZE"] == [
        settings.max_decompressed_bytes,
        settings.max_decompressed_bytes,
    ]
    assert limits["NOFILE"] == [MAX_OPEN_FILES, MAX_OPEN_FILES]
    assert limits["CORE"] == [0, 0]  # no core dumps of resume text
    # Relative to live usage rather than absolute (see `_nproc_limit`), so the
    # assertion is that it is bounded at all, not what it equals.
    soft, _hard = limits["NPROC"]
    assert soft > 0 and soft != resource.RLIM_INFINITY


def test_the_sandbox_is_safe_to_call_from_a_threaded_parent() -> None:
    """The regression the shim exists to prevent (8.4).

    The API parses an uploaded job description from FastAPI's threadpool, so the
    parent is multi-threaded by the time `run_sandboxed` is called. The
    `preexec_fn` this replaced ran Python between `fork` and `exec`, where the
    child holds a snapshot of locks other threads may have been inside — a
    deadlock that shows up as a parse that never returns, under load, rather
    than as an error anyone can reproduce.

    Threads are kept busy for the duration rather than merely started, so they
    are genuinely live across the fork rather than already finished.
    """
    stop = threading.Event()
    workers = [threading.Thread(target=stop.wait, daemon=True) for _ in range(8)]
    for worker in workers:
        worker.start()

    try:
        assert threading.active_count() > 8
        results = [run("ok") for _ in range(3)]
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=5)

    assert all(r.outcome is ParseOutcome.OK for r in results)
    assert all(
        r.payload == {"text": "Asha Nair. Senior Backend Engineer.", "page_count": 1}
        for r in results
    )
