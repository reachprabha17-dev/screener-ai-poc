"""Parser sandbox (spec 8.4). Runs the parser in a separate short-lived process.

A process, not a thread. The parent holds no parser state, so a segfault in a
PDF library kills a child we already expect to lose and the worker carries on to
the next resume. In a thread the same crash takes the worker, the claimed job,
and the run with it.

This module implements the third of the three options in 8.4 — rlimits,
scrubbed environment, per-job tmpdir — which the spec labels the *minimum
acceptable* one. `run_sandboxed` is the seam: a container
(`--network none --read-only --cap-drop ALL --pids-limit`) or a dedicated
unprivileged uid is strictly better, and swapping to either changes nothing
above this module.

**The gap that leaves, stated plainly.** The parser needs no network under any
deployment, and blocking it is the single highest-value control here: it turns
most parser RCE from "compromise and exfiltrate" into "crash a subprocess we
already expect to crash". *This tier does not block it.* Neither rlimits nor an
environment allowlist can — that requires a namespace or a firewall rule, which
is exactly what tiers 1 and 2 buy. Before this pipeline is pointed at real
candidate data on a networked host, deploy one of them. What is implemented here
bounds the blast radius of a crash; it does not bound the blast radius of a
takeover.

Note the limits below cannot be relied on to bound wall-clock time on their own:
``RLIMIT_CPU`` counts CPU seconds, so a child blocked on I/O burns none of it and
would hang forever. The parent's own timeout is what actually bounds the call,
and the rlimit is the backstop for the spin-loop case.
"""

import contextlib
import json
import os
import shutil
import signal
import subprocess  # noqa: S404 — sandboxing a hostile parser is the point of this module
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from config.settings import settings
from screener.models import Flag, ParsedResume, ParseResult

PARSE_WORKER_MODULE = "screener.intake.parse_worker"
# Exec'd in place of the parser, applies the rlimits, then execs the parser.
# See its module docstring for why the limits are not applied here.
RLIMIT_SHIM_MODULE = "screener.intake.rlimit_shim"

# How many *new* processes and threads the parser may create, above whatever the
# service account is already running.
#
# `RLIMIT_NPROC` is a per-real-UID limit counted across the entire system, not a
# per-process one. An absolute small value therefore does not mean "this parser
# may fork 32 times" — it means "this parser may run only if the account owns
# fewer than 32 processes in total", which on any real host it does not. Measured
# here: 158 processes / 1,887 threads already live under the service account, and
# an absolute limit of 32 killed the parser before it read a byte:
#
#     pyo3_runtime.PanicException: OS can't spawn worker thread:
#     Resource temporarily unavailable (os error 11)
#
# So the limit is computed relative to current usage. That preserves what the
# control was for — bounding how much the parser can multiply — while remaining
# survivable on a shared account. Genuine pid containment is `--pids-limit` at
# tier 1 or a dedicated uid at tier 2 (8.4).
MAX_NEW_PROCESSES = 96
MAX_OPEN_FILES = 128

# `RLIMIT_AS` as a multiple of the memory limit. The parser reserves ~2.9 GB of
# address space to hold ~270 MB, so this has to clear the reservation by a wide
# margin or nothing runs. It is a backstop against runaway mapping, not the
# memory control — `RLIMIT_DATA` is that.
ADDRESS_SPACE_HEADROOM = 8

# Under-provisioning memory does **not** produce a clean error. Measured against
# the OCR path: 512 MB segfaults, 256 MB *hangs* until the parent's wall clock
# kills it, and 1024 MB aborts on any document past three pages. 1536 MB is the
# floor for a 20-page scan. Do not lower `parse_mem_limit_mb` without
# re-measuring — the failure mode is a stalled worker, not a flag.
MIN_TESTED_MEM_LIMIT_MB = 1536

# The child gets no inherited environment. PATH is needed for tesseract; the rest
# would leak host configuration into an untrusted parse for no benefit.
_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TESSDATA_PREFIX")

# Grace between the CPU rlimit and the parent's wall clock, so a spinning child
# is attributed to SIGXCPU rather than reaching the parent's timeout first and
# logging as "slow" when it was looping.
_WALL_CLOCK_GRACE_S = 2

# Signals meaning a limit fired. Distinguished from the ones below because they
# say nothing about the file beyond "it was expensive".
_LIMIT_SIGNALS = frozenset({signal.SIGXCPU, signal.SIGXFSZ, signal.SIGKILL})


class ParseOutcome(StrEnum):
    OK = "OK"
    TIMEOUT = "TIMEOUT"
    CRASHED = "CRASHED"
    INVALID_OUTPUT = "INVALID_OUTPUT"


@dataclass(frozen=True)
class SandboxResult:
    outcome: ParseOutcome
    payload: dict[str, Any] | None = None
    stderr: str = ""
    exit_code: int | None = None
    killed_by: int | None = None
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome is ParseOutcome.OK

    @property
    def flags(self) -> list[Flag]:
        """Both are transient (12.5) and must never be cached.

        A timeout cached as a permanent verdict sidelines a real candidate
        forever on the strength of one slow parse.
        """
        if self.outcome is ParseOutcome.OK:
            return []
        if self.outcome is ParseOutcome.TIMEOUT:
            return [Flag.PARSER_TIMEOUT]
        return [Flag.PARSER_CRASHED]

    @property
    def security_event(self) -> bool:
        """A crash is not a data-quality event.

        It signals a file doing something the parser did not expect, and is
        reported as a security finding even when the likely cause is a corrupt
        CV. The alternative — treating crashes as noise — means the one signal
        that a parser exploit is being tried arrives as a routine warning.
        """
        return self.outcome in (ParseOutcome.CRASHED, ParseOutcome.INVALID_OUTPUT)


def _current_uid_threads() -> int | None:
    """Threads currently owned by this real UID, or ``None`` if it cannot be read.

    Read from ``/proc`` because there is no syscall that reports it. Threads
    rather than processes: ``RLIMIT_NPROC`` counts kernel tasks, and the parser's
    Rust runtime creates threads, not forks — counting processes would undershoot
    by an order of magnitude and reintroduce the failure this exists to avoid.
    """
    uid = os.getuid()
    total = 0
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                total += sum(1 for _ in Path(f"/proc/{entry.name}/task").iterdir())
            except OSError:
                # The process exited between listing and reading. Expected.
                continue
    except OSError:
        return None
    return total or None


def _nproc_limit() -> int | None:
    """Current usage plus headroom, or ``None`` to leave the limit alone.

    ``None`` when the count is unavailable: a parser that cannot start is worse
    than one whose process ceiling is only enforced by the outer tier, and an
    unreadable ``/proc`` means any absolute number would be a guess.
    """
    current = _current_uid_threads()
    if current is None:
        return None
    return current + MAX_NEW_PROCESSES


def _limit_spec(cpu_seconds: int, nproc: int | None) -> list[tuple[str, int, int]]:
    """The rlimits, as data. Computed in the parent, applied by ``rlimit_shim``.

    Applied before the parser's own ``execve``, so the limits still cover
    interpreter startup and the parser's imports rather than only the code that
    remembers to opt in — rlimits are not reset by ``execve``.

    This used to be a ``preexec_fn``, which ran it between ``fork`` and ``exec``.
    That window admits only async-signal-safe work, and a Python callback in it
    deadlocks a multi-threaded parent sooner or later. Returning data instead
    means the only thing between fork and exec is C code inside ``subprocess``,
    and the sandbox became safe to call from anywhere — including the API's
    threadpool, which is what the job-description upload does.

    ``nproc`` is still computed by the caller, in the parent: it reads ``/proc``,
    and that is work the shim should not be doing inside a process that is about
    to be handed a hostile document.
    """
    # `RLIMIT_DATA` is the real memory bound, not `RLIMIT_AS`. Modern native
    # runtimes reserve enormous virtual address space they never touch:
    # measured here, a parse peaks at ~2.9 GB of VA while holding ~270 MB
    # resident. An `RLIMIT_AS` set to the intended memory ceiling therefore
    # kills the parser before it reads a byte. Since Linux 4.7 `RLIMIT_DATA`
    # covers private anonymous mappings, so it tracks what is actually
    # allocated — which is what a decompression bomb consumes.
    #
    # `RLIMIT_AS` is kept only as a runaway-address-space backstop, set well
    # above the runtime's reservations.
    data = settings.parse_mem_limit_mb * 1024 * 1024
    spec = [
        ("DATA", data, data),
        ("AS", data * ADDRESS_SPACE_HEADROOM, data * ADDRESS_SPACE_HEADROOM),
        # Soft below hard, so a CPU loop takes SIGXCPU — which is diagnosable —
        # rather than a bare SIGKILL indistinguishable from an OOM kill.
        ("CPU", cpu_seconds, cpu_seconds + 1),
        ("FSIZE", settings.max_decompressed_bytes, settings.max_decompressed_bytes),
        ("NOFILE", MAX_OPEN_FILES, MAX_OPEN_FILES),
        ("CORE", 0, 0),  # no core dumps of resume text
    ]
    if nproc is not None:
        spec.append(("NPROC", nproc, nproc))
    return spec


def _child_env(extra: dict[str, str] | None) -> dict[str, str]:
    """Allowlist, never inheritance.

    The parent's environment holds database paths, the Ollama host, and whatever
    else the operator exported. None of it is useful to a parser and all of it is
    readable by one that has been taken over.

    ``OMP_NUM_THREADS`` is set rather than passed through: the parser's native
    dependencies size their thread pools from the host's core count, which
    collides with ``RLIMIT_NPROC`` on a large machine and fails a parse that
    would otherwise have worked.
    """
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    env["OMP_NUM_THREADS"] = "1"
    env.update(extra or {})
    return env


def _classify(returncode: int) -> ParseOutcome:
    """Negative return codes are signals.

    A limit firing says the file was expensive; anything else says the parser
    was driven into undefined behaviour, which is the case worth waking somebody
    for.
    """
    if returncode < 0 and -returncode in _LIMIT_SIGNALS:
        return ParseOutcome.TIMEOUT
    return ParseOutcome.CRASHED


def _kill_process_group(child: "subprocess.Popen[str]") -> None:
    """SIGKILL the child's whole process group, tolerating a race with its exit.

    Safe only because the child was started with ``start_new_session``: its pgid
    is its own pid, so this cannot reach the caller's group. ``ProcessLookupError``
    is the ordinary case where the child died between the timeout firing and this
    call — not an error, and not worth a log line.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(child.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, OSError):
        child.kill()


def run_sandboxed(
    path: Path,
    *,
    worker_module: str = PARSE_WORKER_MODULE,
    timeout_s: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> SandboxResult:
    """Parse ``path`` in a locked-down subprocess and return its JSON payload.

    ``worker_module`` is injectable so the sandbox's own containment can be
    exercised against deliberately hostile children. A sandbox that has only
    ever run well-behaved input has not been tested — every guarantee it makes
    is about the case where the child misbehaves.

    ``extra_env`` adds to the allowlisted environment. Deployment-specific
    values (a tessdata path on a container image, a test harness's
    ``PYTHONPATH``) go here rather than widening the allowlist, so the default
    stays sealed.
    """
    timeout = timeout_s if timeout_s is not None else settings.parse_timeout_s
    # Computed here, in the parent: the post-fork window admits only
    # async-signal-safe work, and this reads /proc.
    nproc = _nproc_limit()
    tmpdir = tempfile.mkdtemp(prefix="screener-parse-")
    started = time.monotonic()

    try:
        # `Popen` rather than `run`, for the pid alone. `run`'s timeout path
        # kills one process; the group kill below needs to name the group, and
        # `TimeoutExpired` does not carry the pid to name it with.
        child = subprocess.Popen(  # noqa: S603 — fixed argv, no shell, scrubbed env
            [
                sys.executable,
                "-m",
                RLIMIT_SHIM_MODULE,
                json.dumps(_limit_spec(timeout, nproc)),
                worker_module,
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # cwd is a fresh per-job directory: anything the parser writes
            # relative to itself lands somewhere we delete. Survives the shim's
            # `execve`, so it is the parser's cwd too.
            cwd=tmpdir,
            env=_child_env(extra_env),
            # setsid, performed by `_posixsubprocess` in C between fork and exec.
            # Async-signal-safe, unlike the `preexec_fn` this replaced, and it
            # covers the shim as well as the parser it execs.
            start_new_session=True,
        )
        try:
            stdout, stderr = child.communicate(timeout=timeout + _WALL_CLOCK_GRACE_S)
        except subprocess.TimeoutExpired:
            # Kill the *group*, not the process. The parser's own children
            # (tesseract, or anything a hostile file persuaded it to fork)
            # outlive a single-pid kill, holding the CPU we just tried to
            # reclaim and the stdout pipe we are still reading — which is a
            # parent that hangs after its own timeout fired.
            #
            # `start_new_session` above is what makes this safe as well as
            # necessary: the child leads its own session, so its pgid is its own
            # pid and the signal cannot reach the caller's process group. That
            # was not true before, which is why this was previously asserted in
            # a comment rather than done.
            _kill_process_group(child)
            # Drains rather than blocks: every writer is dead by now.
            _, stderr = child.communicate()
            return SandboxResult(
                outcome=ParseOutcome.TIMEOUT,
                stderr=_as_text(stderr)[-4000:],
                duration_s=round(time.monotonic() - started, 3),
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    duration = round(time.monotonic() - started, 3)
    returncode = child.returncode

    if returncode != 0:
        return SandboxResult(
            outcome=_classify(returncode),
            stderr=stderr[-4000:],
            exit_code=returncode if returncode > 0 else None,
            killed_by=-returncode if returncode < 0 else None,
            duration_s=duration,
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        # Exit 0 with unreadable output means the contract between parent and
        # child broke. Treated as a crash, not as an empty parse: an empty parse
        # is a cacheable statement about the document, and this is not one.
        return SandboxResult(
            outcome=ParseOutcome.INVALID_OUTPUT,
            stderr=f"{exc}\n{stderr[-4000:]}",
            exit_code=0,
            duration_s=duration,
        )

    if not isinstance(payload, dict):
        return SandboxResult(
            outcome=ParseOutcome.INVALID_OUTPUT,
            stderr=f"expected a JSON object, got {type(payload).__name__}",
            exit_code=0,
            duration_s=duration,
        )

    return SandboxResult(
        outcome=ParseOutcome.OK,
        payload=payload,
        stderr=stderr[-4000:],
        exit_code=0,
        duration_s=duration,
    )


def _as_text(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


# --- the ResumeParser port ---------------------------------------------------


class SandboxedParser:
    """Satisfies ``ports.ResumeParser``. Every parse is a separate process.

    The subprocess is an implementation detail of this class. `pipeline.py` asks
    for text and gets text or a flag; it never learns that a process was forked,
    which is what lets the container and dedicated-uid tiers of 8.4 drop in
    without touching a single caller.

    Maps the 8.5 failure table, with one refinement the table does not draw:
    a *clean* parser error — the parser read the file, found it malformed, and
    said so — is `EXTRACTION_FAILED`, not `PARSER_CRASHED`. It is a deterministic
    property of the document, so it is cacheable, and it is a data-quality event
    rather than a security finding. Only a parser that died without reporting
    gets treated as an attack.
    """

    def parse(self, path: Path, *, timeout_s: int | None = None) -> ParseResult:
        result = run_sandboxed(path, timeout_s=timeout_s)

        if result.outcome is ParseOutcome.TIMEOUT:
            return ParseResult(
                flags=[Flag.PARSER_TIMEOUT],
                error="parser exceeded its time or CPU limit",
                duration_s=result.duration_s,
            )

        if result.outcome is not ParseOutcome.OK:
            return ParseResult(
                flags=[Flag.PARSER_CRASHED],
                error=result.stderr[-1000:] or str(result.outcome),
                duration_s=result.duration_s,
            )

        payload = result.payload or {}
        if not payload.get("ok"):
            return ParseResult(
                flags=[Flag.EXTRACTION_FAILED],
                error=str(payload.get("error", "parser reported failure"))[:1000],
                duration_s=result.duration_s,
            )

        text = str(payload.get("text") or "")
        page_count = int(payload.get("page_count") or 0)

        if not text.strip():
            # OCR has already been attempted by this point, so an empty result is
            # a statement about the document rather than a missing capability.
            return ParseResult(
                flags=[Flag.EXTRACTION_FAILED],
                error="no text extracted",
                duration_s=result.duration_s,
            )

        if page_count > settings.max_pages:
            # The real page-bomb enforcement. `validate_file` screens what it can
            # read without parsing; a 1.5+ page tree inside compressed object
            # streams is only countable here, after the parse it was guarding.
            return ParseResult(
                flags=[Flag.INPUT_REJECTED],
                error=f"{page_count} pages exceeds max_pages={settings.max_pages}",
                duration_s=result.duration_s,
            )

        return ParseResult(
            parsed=ParsedResume(
                text=text,
                page_count=page_count,
                ocr_used=bool(payload.get("ocr_used")),
                warnings=[str(w) for w in (payload.get("warnings") or [])],
                parser_version=str(payload.get("parser_version") or "unknown"),
            ),
            duration_s=result.duration_s,
        )
