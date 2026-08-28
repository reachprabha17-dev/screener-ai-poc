"""Documentation gates (spec 4, 7; `screener_spec_v8.md`).

Same argument as `test_layering.py`, applied one level out. That module exists
because prose decays — an import added under time pressure works, the tests pass,
and six months later the architecture the docs describe is not the one that runs.
The docs decay the same way and for the same reason, except that nothing was
checking them at all.

**Three things went wrong on one change**, and each is a gate below:

- Three files were added under `web/src` and the guide that claims to describe
  *every file* in that tree did not mention them.
- `tests/test_layering.py` was amended without amending §4, so the enforcement
  and the specification disagreed — in a module whose own docstring says the
  rules are "written in 4 as prose".
- A new endpoint was added to `client.ts` and not to `API_PATHS` in
  `vite.config.ts`, which fails *silently*: the dev server answers with
  `index.html` and a 200. (That one is guarded in `test_layering.py`, beside the
  rule about the client being the only network I/O.)

**These are coverage checks, not quality checks.** They assert that a thing is
*mentioned*, which is the most a test can know. A file named in a stale sentence
passes. What they buy is that nobody adds a module, a setting or a route without
being made to open the document that describes it — and opening it is the step
that was being skipped.

Each gate names one document and one directory, so a failure says exactly which
file to edit.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

UI_GUIDE = DOCS / "UI_LAYER_GUIDE.md"
ENV_EXAMPLE = ROOT / ".env.example"


def _docs_text() -> str:
    """Every design document, concatenated.

    A module may be described wherever it belongs — v6 for the original design,
    v7 or v8 for what amended it, the guide for how to read it. The gate is that
    it is described *somewhere*, not that it is described in a particular file.
    """
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(DOCS.glob("*.md")))


def test_every_interface_source_file_appears_in_the_ui_guide() -> None:
    """`UI_LAYER_GUIDE.md` claims to cover "every file" under `web/`. Hold it to that.

    Part 3 is a file map and Part 12 a table of primitives, so a new component
    that appears in neither is a component nobody reading the guide knows exists.
    Matched on the stem rather than the filename because the primitives are
    listed as bare names (`Alert  Badge  Button …`), which is the shape that
    table wants.

    Tests are excluded: Part 15 lists them and does so selectively, on purpose —
    it explains what each *interesting* suite covers rather than enumerating
    files.
    """
    sources = sorted(
        path
        for path in (ROOT / "web/src").rglob("*.ts*")
        if not path.name.endswith((".test.ts", ".test.tsx"))
    )
    assert sources, "no interface sources found — has web/src moved?"

    guide = UI_GUIDE.read_text(encoding="utf-8")
    missing = sorted(str(p.relative_to(ROOT)) for p in sources if p.stem not in guide)

    assert not missing, (
        f"{missing} exist under web/src but are named nowhere in "
        f"{UI_GUIDE.relative_to(ROOT)} — add them to the file map in Part 3, and "
        "to Part 12 if they are primitives"
    )


def test_every_setting_appears_in_the_env_example() -> None:
    """`.env.example` is the only place an operator learns a setting exists.

    `config/settings.py` has defaults for everything, so a setting missing here
    is not a startup failure — it is a knob that silently cannot be found by the
    person deploying this. That includes the ones whose *default* is the
    interesting decision: `escalation_budget` is deliberately unset, and saying
    so in the file an operator reads is the entire point of it being unset.

    Read with `ast` rather than a regex: a regex over `name: type = value` also
    matches annotated locals inside methods, which is how `try` once appeared in
    a list of configuration fields.
    """
    tree = ast.parse((ROOT / "config/settings.py").read_text(encoding="utf-8"))
    settings_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )
    fields = [
        node.target.id
        for node in settings_class.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    ]
    assert fields, "no settings fields found — has the Settings class moved?"

    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    missing = sorted(name for name in fields if name.upper() not in example)

    assert not missing, (
        f"{missing} are settings with no mention in {ENV_EXAMPLE.name} — an "
        "operator cannot configure what is not written down there"
    )


def test_every_intake_module_is_described_in_the_docs() -> None:
    """`screener/intake/` is the code that touches hostile bytes. It is documented.

    Scoped to this one package rather than all of `screener/`, and that is a
    deliberate limit rather than an oversight: §4's tree writes other families in
    brace form (`llm/{extract_rubric,judge_resume,…}.py`, `api/{app,deps}.py +
    routes/`), so their individual filenames legitimately do not appear and a
    literal check would fail on fifteen modules nobody has touched. Expanding
    that notation here would make this gate a parser for a documentation
    convention, which is more machinery than it is worth.

    Intake earns the check on its own: every module in it is enumerated
    one-per-line in §4 with its spec reference, it is the only package where a
    change alters what a malicious document can do, and §8.4's containment claims
    are only as good as the description of what implements them.
    """
    modules = sorted(
        path for path in (ROOT / "screener/intake").glob("*.py") if path.stat().st_size > 0
    )
    assert modules, "no intake modules found — has the package moved?"

    docs = _docs_text()
    missing = sorted(p.name for p in modules if p.name not in docs)

    assert not missing, (
        f"{missing} are in screener/intake/ and named in no document under docs/ "
        "— this is the package that reads untrusted files, and §8.4's guarantees "
        "are only as good as the account of what implements them"
    )


@pytest.mark.parametrize(
    ("document", "why"),
    [
        (DOCS / "screener_spec_v6.md", "the design"),
        (DOCS / "screener_spec_v7.md", "the reviewer interface"),
        (DOCS / "screener_spec_v8.md", "job descriptions as documents"),
        (DOCS / "CODEBASE_GUIDE.md", "the way in"),
        (UI_GUIDE, "the interface, end to end"),
    ],
)
def test_the_documents_the_other_gates_depend_on_exist(document: Path, why: str) -> None:
    """A renamed or deleted document must fail here, not silently pass elsewhere.

    Every gate above searches text. A missing file would make that text empty,
    and an empty haystack makes a "was it mentioned?" check fail loudly rather
    than quietly — but only if something asserts the haystack was real. This is
    that assertion.
    """
    assert document.is_file(), f"{document.name} ({why}) is missing"
    assert document.stat().st_size > 0, f"{document.name} is empty"
