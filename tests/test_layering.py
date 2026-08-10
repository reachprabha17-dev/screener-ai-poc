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
- **`ui/` must not import `sqlite3` or `screener.*`.** This is the real
  enforcement of decision #11 — 3.1's claim that the dependency graph does it is
  wrong, because `sqlite3` ships with Python and cannot be uninstalled.
- **`core/` must do no I/O.** It is why the scoring, ranking and evidence rules
  are testable without a GPU or a database.
"""

import ast
import importlib
from collections.abc import Iterable
from pathlib import Path

import pytest

from screener import ports

ROOT = Path(__file__).resolve().parent.parent

# Modules that touch the outside world. `core/` may import none of them.
IO_MODULES = (
    "sqlite3",
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


def test_no_api_module_can_screen() -> None:
    assert_forbidden(package_imports("screener/api"), ("screener.pipeline", "screener.intake"))


def test_the_api_reaches_storage_only_through_the_service() -> None:
    """Routes parse, authorize, delegate, serialize (15.4).

    A route holding a transaction is a business rule the CLI and worker cannot
    reach.
    """
    assert_forbidden(
        package_imports("screener/api/routes", "screener/api/deps.py"), ("screener.storage",)
    )


def test_the_only_storage_import_in_the_api_is_the_startup_gate() -> None:
    """One documented exception, and it is not request handling.

    `create_app` calls `require_current_schema()` before serving anything: the
    process must refuse to start against a stale schema, and a schema/code
    mismatch on candidate decisions is an integrity incident, not a warning
    (12.1). That is a **bootstrap** concern, the same category as
    `cli.py migrate` — it necessarily precedes the service layer rather than
    routing around it.

    Pinned narrowly so the exception cannot widen: `connection` only, `app.py`
    only, and the routes are held to the strict rule above.
    """
    storage = {
        path: sorted(m for m in modules if m.startswith("screener.storage"))
        for path, modules in package_imports("screener/api").items()
    }
    offenders = {path: modules for path, modules in storage.items() if modules}

    assert offenders == {Path("screener/api/app.py"): ["screener.storage.connection"]}


# --- decision #11: the UI is an HTTP client and nothing else -----------------


def test_the_ui_imports_no_database_driver() -> None:
    """The real enforcement of decision #11.

    3.1 claims the dependency graph does this. It does not — `sqlite3` is in the
    standard library and cannot be uninstalled. Omitting a driver from the `ui`
    extra removes the temptation; **this test is what removes the possibility**.
    """
    assert_forbidden(package_imports("ui"), ("sqlite3",))


def test_the_ui_imports_nothing_from_the_screener_package() -> None:
    """Stronger than 4 requires, and worth keeping.

    Sharing domain models would tie UI deploys to server deploys and reopen a
    transitive path to the storage layer.
    """
    assert_forbidden(package_imports("ui"), ("screener", "config"))


def test_the_ui_talks_over_http() -> None:
    """It has to reach the data somehow. HTTP is the only sanctioned route."""
    combined: set[str] = set()
    for modules in package_imports("ui").values():
        combined |= modules

    assert "httpx" in combined


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
        ("sqlite3", "screener.storage.results_store", "screener.storage.jobs_store"),
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
