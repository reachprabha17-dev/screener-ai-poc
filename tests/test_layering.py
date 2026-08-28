"""Architecture, enforced (spec 4, build gate 20 step 20).

Every rule below is written in 4 as prose. Prose decays: someone adds an import
under time pressure, it works, the tests pass, and six months later the UI holds
a database connection and the service layer runs inference inside a request.
This module is the difference between an architecture that is *described* and one
that is *enforced*.

Read from the **import graph**, never from source text. A substring search cannot
tell a violation from a docstring explaining why the violation would be bad — a
lesson from step 15, where `runs.py`'s own explanation of why `BackgroundTasks`
is forbidden failed the test that forbade it.

Three rules carry most of the weight:

- **`service.py` must not import `pipeline.py`.** It enqueues; the worker
  executes. Collapse that and a 1,000-CV batch runs inside an HTTP request.
- **The reviewer interface reaches the data only over HTTP.** Under Streamlit
  that meant `ui/` importing neither `sqlite3` nor `screener.*`; the interface is
  a React app now, so it means every request passes through one client module and
  nothing renders raw HTML. The process that could have opened the database no
  longer exists.
- **`core/` must do no I/O.** It is why the scoring, ranking and evidence rules
  are testable without a GPU or a database.
"""

import ast
import importlib
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

from screener import ports

ROOT = Path(__file__).resolve().parent.parent

# Modules that touch the outside world. `core/` may import none of them.
IO_MODULES = (
    "sqlite3",
    "psycopg",
    "sqlalchemy",
    "socket",
    "subprocess",
    "requests",
    "httpx",
    "urllib",
    "ollama",
    "xberg",
    "fastapi",
    "streamlit",
)


def imports_of(path: Path) -> set[str]:
    """Every module named by an import in this file, including deferred ones.

    Function-level imports count. A rule that only inspected the top of the file
    would be satisfied by moving the offending import inside a function, which is
    exactly what someone under pressure would do.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def package_imports(*relative: str) -> dict[Path, set[str]]:
    """Import sets for every Python file under the given paths."""
    result: dict[Path, set[str]] = {}
    for entry in relative:
        target = ROOT / entry
        files: Iterable[Path] = [target] if target.is_file() else sorted(target.rglob("*.py"))
        for path in files:
            if path.name == "__init__.py" and path.stat().st_size == 0:
                continue
            result[path.relative_to(ROOT)] = imports_of(path)
    return result


def assert_forbidden(paths: dict[Path, set[str]], forbidden: tuple[str, ...]) -> None:
    for path, modules in paths.items():
        offending = sorted(m for m in modules if m.startswith(forbidden))
        assert not offending, f"{path} imports {offending}"


# --- the boundary that keeps the API responsive ------------------------------


def test_the_service_layer_never_imports_the_pipeline() -> None:
    """14's hard rule: it enqueues, the worker executes.

    Screening inside a request lifecycle loses orphan reclaim and resumption and
    dies with the process — and it would work perfectly on three files before
    falling over on a thousand.
    """
    assert_forbidden(
        package_imports("screener/service.py"),
        ("screener.pipeline", "screener.intake"),
    )


def test_the_service_layer_reaches_the_parser_only_through_a_port() -> None:
    """How `service.extract_jd_document` reads a document without importing one.

    The test above forbids `screener.intake` in `service.py` and that has not
    been relaxed — the service layer runs inside the API process, and keeping
    document parsing out of it is worth more than a direct call. So the seam is
    `ports.DocumentExtractor`, wired at the composition root, exactly as
    `ports.LLMClient` has always been.

    Asserted rather than left to the ban, because the ban is satisfied just as
    well by a service that cannot read a document at all, and that is a different
    system from this one.
    """
    source = (ROOT / "screener/service.py").read_text(encoding="utf-8")

    assert isinstance(ports.DocumentExtractor, type)
    assert "extractor: DocumentExtractor | None" in source
    assert "screener.ports" in imports_of(ROOT / "screener/service.py")


# The one module allowed to name concrete infrastructure. It is where
# `OllamaClient` is constructed, and it is the only place a class from
# `screener.intake` may be named.
COMPOSITION_ROOT = Path("screener/api/deps.py")


def test_no_api_module_can_screen() -> None:
    """`screener.pipeline` is absolute; `screener.intake` is not, and was.

    These were one rule and they protect two different things.

    **The pipeline ban is about work.** Screening inside a request lifecycle
    loses orphan reclaim and resumption and dies with the process — and it would
    work perfectly on three files before falling over on a thousand. Nothing in
    `api/` may import it, ever, including the composition root.

    **The intake ban was about hostile bytes** in the process holding the
    database credentials. That is a real concern and it is not this ban that
    addresses it: `sandbox.py` does, by running every parse in a separate
    rlimited process with a scrubbed environment. Banning the import as well was
    doing the sandbox's job a second time, and the collateral was a legitimate
    use — the API extracting text from one uploaded job description, one
    document, while a human waits, which is the same shape as `extract_rubric`
    and not the shape of a batch.

    So the ban narrows to what it was protecting. `deps.py` may construct the
    extractor, exactly as it constructs `OllamaClient`; every other module under
    `api/` still may not import intake at all, and no module under `api/` may
    import the pipeline. `service.py` keeps **both** bans (below) and reaches the
    parser through `ports.DocumentExtractor`.

    If this test is failing because a route module wants to parse something, the
    fix is a service method behind the port, not another name in
    `COMPOSITION_ROOT`.
    """
    api = package_imports("screener/api")

    assert_forbidden(api, ("screener.pipeline",))

    intake = {
        path: sorted(m for m in modules if m.startswith("screener.intake"))
        for path, modules in api.items()
        if path != COMPOSITION_ROOT
    }
    assert {path: mods for path, mods in intake.items() if mods} == {}


def test_the_composition_root_names_infrastructure_and_nothing_else_does() -> None:
    """The exception above is real, so it is asserted rather than assumed.

    A narrowed rule that nobody checks the narrow end of is a deleted rule. This
    fails if `deps.py` stops being the place the extractor is wired — which is
    the state in which the exception has stopped paying for itself.
    """
    imports = imports_of(ROOT / COMPOSITION_ROOT)

    assert "screener.intake.document_text" in imports
    assert "screener.clients.ollama_client" in imports


def test_the_api_reaches_storage_only_through_the_service() -> None:
    """Routes parse, authorize, delegate, serialize (15.4).

    A route holding a transaction is a business rule the CLI and worker cannot
    reach.
    """
    assert_forbidden(
        package_imports("screener/api/routes", "screener/api/deps.py"), ("screener.storage",)
    )


def test_no_api_module_imports_storage_at_all() -> None:
    """4's `api/ → service.py, schemas.py, models.py`, with no exceptions.

    There used to be one: `app.py` imported `require_current_schema` directly,
    on the reasoning that a startup gate is a bootstrap concern that precedes
    the service layer. That did not survive examination — `service.py` already
    imports `storage.connection`, and importing a module constructs nothing, so
    the gate runs just as early behind `service.require_current_schema`. The
    exception bought nothing and cost the rule its absoluteness.

    An absolute rule is worth more than a narrow one here. A pinned exception
    invites the next one to argue it is the same category — which is exactly the
    argument that was made for putting the per-request connection release in
    `deps.py`, and it was wrong too: that went behind
    `service.release_connection` instead (`screener/api/routing.py`).

    So: nothing under `screener/api` imports `screener.storage`. If this test
    fails, the fix is a delegate on `service.py`, not an entry in a list here.
    """
    storage = {
        path: sorted(m for m in modules if m.startswith("screener.storage"))
        for path, modules in package_imports("screener/api").items()
    }
    offenders = {path: modules for path, modules in storage.items() if modules}

    assert offenders == {}


# --- decision #11: the UI is an HTTP client and nothing else -----------------
#
# The reviewer interface is a React app under `web/`, so these four rules are
# read from source text rather than from an import graph. That is a real
# weakening of the method used everywhere else in this module and it is worth
# stating plainly: a substring search cannot tell a violation from a comment
# describing one — and the raw-HTML rule below caught its own explanatory comment
# on the first run, which is step 15's `BackgroundTasks` lesson repeating itself.
# The mitigation is to keep every pattern lexically unambiguous and to write
# about the forbidden identifiers without naming them.
#
# What has *strengthened* is the boundary itself. Under Streamlit the UI was a
# Python process that could `import sqlite3`; the test existed because the
# dependency graph could not stop it. A browser cannot open the database under
# any circumstances, so the rules below are about the app's own discipline
# rather than about what it is capable of reaching.

WEB_SRC = ROOT / "web" / "src"

# The single module allowed to perform network I/O.
API_CLIENT = "web/src/api/client.ts"


def web_sources() -> dict[Path, str]:
    """Every TypeScript source under `web/src`, as text."""
    return {
        path.relative_to(ROOT): path.read_text(encoding="utf-8")
        for path in sorted(WEB_SRC.rglob("*.ts*"))
    }


def test_the_reviewer_ui_is_not_a_python_process() -> None:
    """Streamlit is gone, and nothing may quietly bring a server-side UI back.

    The whole of decision #11's original risk was that the UI ran in a Python
    process next to the database. It no longer runs one — and a `.py` file under
    `web/`, or a `streamlit` import anywhere, is that risk returning.
    """
    assert not list(WEB_SRC.rglob("*.py")), "the reviewer interface is a browser app"

    server_side = {
        path: sorted(m for m in modules if m.startswith("streamlit"))
        for path, modules in package_imports("screener", "config", "eval", "worker.py").items()
    }
    assert not {path: mods for path, mods in server_side.items() if mods}


def test_every_network_call_goes_through_the_one_client() -> None:
    """`fetch` appears in exactly one file.

    This is the browser-side replacement for "the UI imports no database driver".
    Every request has to pass through `api/client.ts`, because that is where an
    unreachable API becomes a sentence a recruiter can act on instead of a
    `TypeError: Failed to fetch` rendered over candidate data — and where the
    identity headers are attached, which is what the audit log records.
    """
    # `\bfetch\(` and not `"fetch("`: the substring form matches TanStack Query's
    # `refetch()`, which is a cache instruction rather than network I/O. The
    # first run of this test failed on exactly that.
    calls = re.compile(r"\bfetch\(|XMLHttpRequest|\baxios\b")
    offenders = [
        str(path)
        for path, source in web_sources().items()
        if str(path) != API_CLIENT
        and not str(path).endswith((".test.ts", ".test.tsx"))
        and calls.search(source)
    ]

    assert not offenders, f"network I/O outside {API_CLIENT}: {offenders}"


def test_every_path_the_client_calls_is_reachable_in_development() -> None:
    """`client.ts` and `vite.config.ts` must name the same API paths.

    The dev server proxies an **allowlist**, and an unlisted path does not fail
    loudly — Vite falls through to its single-page-app handler and answers the
    API call with `index.html` and a 200. The browser then receives a web page
    where it expected JSON, nothing reaches the API at all, and its access log
    stays empty while the interface reports a broken endpoint. That is a long
    way from the missing line that caused it.

    This is the gap that let `/jd-documents` and `/config` ship broken: the
    Python suite reaches the app through `TestClient`, which never opens a
    socket, and the vitest suite stubs `fetch`, so no test on either side of the
    boundary could see a proxy. **The two suites agreed with each other and
    both were wrong about the thing in between them.**

    A superset is fine — `/ready` is proxied and never called from the app —
    but every path the client *can* reach must be listed.
    """
    client = (ROOT / API_CLIENT).read_text(encoding="utf-8")
    config = (ROOT / "web/vite.config.ts").read_text(encoding="utf-8")

    # The first segment is what the proxy matches on, so that is what is
    # compared: `/positions/{id}/rubric` is reachable because `/positions` is.
    called = {
        match.group(1)
        for match in re.finditer(r"""request<[^>]*>\(\s*[`'"](/[A-Za-z0-9._-]+)""", client)
    }
    assert called, "no request paths found — has the call shape in client.ts changed?"

    proxied = set(re.findall(r"""^\s*['"](/[A-Za-z0-9._-]+)['"],""", config, re.MULTILINE))
    assert proxied, "no API_PATHS found — has the shape of vite.config.ts changed?"

    missing = sorted(called - proxied)
    assert not missing, (
        f"{missing} reachable from {API_CLIENT} but absent from API_PATHS in "
        "web/vite.config.ts — the dev server will answer these with index.html "
        "and a 200 instead of proxying them to the API"
    )


def test_the_reviewer_ui_renders_no_raw_html() -> None:
    """Candidate text, decision reasons and audit details all reach the screen.

    Every one of them is attacker-influenced — a resume is a hostile document by
    assumption (8) — so nothing in this app is allowed to hand a string to the
    DOM as markup. The evidence highlighting that used `unsafe_allow_html` under
    Streamlit is React elements here.
    """
    offenders = [
        str(path) for path, source in web_sources().items() if "dangerouslySetInnerHTML" in source
    ]

    assert not offenders, f"raw HTML injection in {offenders}"


def test_the_reviewer_ui_only_talks_to_the_host_that_served_it() -> None:
    """No absolute URLs, so there is no second host to configure or to leak to.

    The Streamlit UI had an "API URL" box in its sidebar. An operator who can
    point the interface at another machine can point candidate data at another
    machine; same-origin removes the setting rather than documenting it.
    """
    offenders = [
        str(path)
        for path, source in web_sources().items()
        if not str(path).endswith((".test.ts", ".test.tsx"))
        and re.search(r"""["'`]https?://""", source)
    ]

    assert not offenders, f"absolute URLs in {offenders}"


# --- core/ is pure -----------------------------------------------------------


def test_core_does_no_io() -> None:
    """Why the scoring and evidence rules are testable without a GPU.

    Every decision that affects a person lives here, and every one of them is a
    pure function over data.
    """
    assert_forbidden(package_imports("screener/core"), IO_MODULES)


def test_core_does_not_reach_into_infrastructure() -> None:
    assert_forbidden(
        package_imports("screener/core"),
        ("screener.storage", "screener.clients", "screener.intake", "screener.service"),
    )


def test_core_may_read_settings_but_nothing_else_stateful() -> None:
    """Two documented deviations from 4's "models.py only".

    **`config.settings`** for thresholds — `evidence_match_ratio`,
    `band_thresholds`, `max_resume_tokens`. Those are policy, not state, and
    inlining them would put tuning constants in three files instead of one.

    **Each other**, since v6. The alternative is worse in both places it comes
    up. `detect_negation` reads the window before a match block, which means it
    must tokenize exactly as `verify_evidence` did — a second tokenizer is a
    second thing that must agree forever, and the symptom of disagreement is a
    highlight off by one word, which reads as a rendering quirk and is never
    reported. `reconcile_judge` re-runs stage B over the verifier's own quote
    (10.6 B), which *is* `verify_evidence` — reimplementing it would mean the
    check that stops one hallucination overriding another drifting away from the
    check it is supposed to be.

    The constraint that still holds is that all of it is pure and acyclic; the
    next test enforces the second half.
    """
    allowed = ("screener.models", "config.settings", "screener.core.")
    for path, modules in package_imports("screener/core").items():
        internal = [m for m in modules if m.startswith(("screener", "config"))]
        assert all(m.startswith(allowed) for m in internal), f"{path}: {internal}"


def test_core_modules_do_not_import_in_a_cycle() -> None:
    """Pure functions calling pure functions is fine; a cycle is not.

    Import cycles inside `core/` would make the decision path circular, and the
    thing that makes these modules testable in a REPL against a literal is that
    each one bottoms out.
    """
    graph = {
        path.stem: {
            m.removeprefix("screener.core.")
            for m in modules
            if m.startswith("screener.core.") and m != "screener.core"
        }
        for path, modules in package_imports("screener/core").items()
    }

    seen: set[str] = set()

    def visit(module: str, stack: tuple[str, ...]) -> None:
        assert module not in stack, f"import cycle: {' -> '.join([*stack, module])}"
        if module in seen:
            return
        seen.add(module)
        for dependency in sorted(graph.get(module, ())):
            visit(dependency, (*stack, module))

    for module in sorted(graph):
        visit(module, ())


# --- models and ports are the bottom of the graph ----------------------------


def test_models_imports_nothing_internal() -> None:
    """The contract every layer agrees on. It cannot depend on any of them."""
    internal = [
        m for m in imports_of(ROOT / "screener/models.py") if m.startswith(("screener", "config"))
    ]

    assert internal == []


def test_ports_imports_only_models() -> None:
    internal = [
        m for m in imports_of(ROOT / "screener/ports.py") if m.startswith(("screener", "config"))
    ]

    assert internal == ["screener.models"]


# --- pipeline and worker -----------------------------------------------------


def test_the_pipeline_never_touches_storage() -> None:
    """It receives collaborators through `Deps` and returns a `Candidate`.

    That is what lets the whole screening path be exercised against fakes with no
    database and no GPU.
    """
    assert_forbidden(
        package_imports("screener/pipeline.py"),
        ("screener.storage", "screener.service", "screener.api"),
    )


def test_the_worker_owns_no_sql() -> None:
    """12.2: the transaction boundary is the service layer.

    A daemon holding its own transactions is a second place where a saved result
    and its closed job can drift apart.
    """
    assert_forbidden(
        package_imports("screener/worker_loop.py", "worker.py"),
        (
            "sqlite3",
            "psycopg",
            "sqlalchemy",
            "screener.storage.results_store",
            "screener.storage.jobs_store",
        ),
    )


def test_the_cli_is_not_an_http_client() -> None:
    """The break-glass path exists for when the API is down."""
    assert_forbidden(
        package_imports("screener/cli.py"), ("httpx", "requests", "fastapi", "screener.api")
    )


# --- ports are declarations that must stay true ------------------------------

STORE_PROTOCOLS = {
    "screener.storage.results_store": ("get_cached", "save", "purge_candidate"),
    "screener.storage.jobs_store": (
        "snapshot_folder",
        "claim_next",
        "heartbeat",
        "complete",
        "fail",
        "reclaim_orphaned",
    ),
}


@pytest.mark.parametrize(("module_name", "required"), STORE_PROTOCOLS.items())
def test_each_store_provides_what_its_protocol_declares(
    module_name: str, required: tuple[str, ...]
) -> None:
    """6's Protocols are declarations; this is what makes them checkable.

    `ResultsStore` and `JobQueue` are **not** used as type annotations anywhere —
    `service.py` imports the concrete modules — so `mypy --strict` would not
    notice a store drifting from its Protocol. 6 is explicit that storage
    portability is therefore *not* the one-file swap the inference ports are.

    This test is the cheap half of the fix: it keeps the declaration honest
    without adding indirection for a Postgres migration that is deferred (21).
    """
    module = importlib.import_module(module_name)

    missing = [name for name in required if not callable(getattr(module, name, None))]
    assert not missing, f"{module_name} is missing {missing} declared by its Protocol"


def test_the_llm_and_parser_ports_are_really_used() -> None:
    """These two *are* the portability mechanism (6), unlike the store ports.

    Swapping Ollama for vLLM is one new file precisely because callers are typed
    against the Protocol rather than the class.
    """
    assert isinstance(ports.LLMClient, type)
    assert isinstance(ports.ResumeParser, type)

    pipeline_source = (ROOT / "screener/pipeline.py").read_text(encoding="utf-8")
    assert "parser: ResumeParser" in pipeline_source
    assert "llm: LLMClient" in pipeline_source


# --- the graph has no cycles -------------------------------------------------


def test_the_layer_graph_is_acyclic() -> None:
    """A cycle means two layers are really one, whatever the folders say."""
    layers = {
        "models": package_imports("screener/models.py"),
        "ports": package_imports("screener/ports.py"),
        "core": package_imports("screener/core"),
        "intake": package_imports("screener/intake"),
        "clients": package_imports("screener/clients"),
        "storage": package_imports("screener/storage"),
        "pipeline": package_imports("screener/pipeline.py"),
        "service": package_imports("screener/service.py"),
        "api": package_imports("screener/api"),
    }
    # Each layer may only import from those below it.
    order = [
        "models",
        "ports",
        "core",
        "intake",
        "clients",
        "storage",
        "pipeline",
        "service",
        "api",
    ]
    rank = {name: index for index, name in enumerate(order)}

    for name, files in layers.items():
        for path, modules in files.items():
            for module in modules:
                if not module.startswith("screener."):
                    continue
                part = module.split(".")[1].removesuffix(".py")
                other = part if part in rank else None
                if other is None or other == name:
                    continue
                assert rank[other] < rank[name], (
                    f"{path} ({name}) imports {module} ({other}) — that is upward "
                    "or sideways in the layer graph"
                )
