"""Tests for core/resume_paths.py (spec intake share).

A reference is a path *relative to the share root*, so `/`-separated names are
valid and everything that could leave the root is not. The traversal cases are
the point of the file: the picker constrains what a reviewer can choose, but the
API accepts whatever is posted to it, so these run against the validator itself.
"""

from pathlib import Path

import pytest

from config.settings import settings
from screener.core.resume_paths import folder_for, is_safe_reference


def test_is_safe_reference_accepts_valid_names() -> None:
    valid_names = [
        "Depot Senior Technician",
        "engineering-2026",
        "eng_1",
        "A",
        "Backend Developer 2026_v1-final",
        "v1.2",
    ]
    for name in valid_names:
        assert is_safe_reference(name) is True, name


def test_is_safe_reference_accepts_ordinary_share_punctuation() -> None:
    """Real folders on a recruiter's share carry these; rejecting them stranded
    the picker, which lists every directory whatever its name.
    """
    valid_names = [
        "test cv -Executive Assistant & Office Manager",
        "R&D",
        "Ops (EU)",
        "Smith's Team",
        "C++ Dev",
        "#1 Team",
        "Sales, Marketing",
        "_Archive 2026",
        "ops_",
    ]
    for name in valid_names:
        assert is_safe_reference(name) is True, name


def test_is_safe_reference_rejects_punctuation_outside_the_allowlist() -> None:
    """Guards the two character-class strings against an accidental range.

    `[…+#-]` is only safe while the `-` stays last. A future edit that reorders
    them into `#-+` would silently admit a span of ASCII, and every case above
    would still pass.
    """
    invalid_names = ["Ops [EU]", "100% Ops", "a@b", "a=b", "a;b", "a|b", "a*b", "a$b", "a!b", "a~b"]
    for name in invalid_names:
        assert is_safe_reference(name) is False, name


def test_is_safe_reference_rejects_space_or_dot_at_a_segment_boundary() -> None:
    """Windows strips trailing spaces and dots, so `eng ` and `eng` are one
    folder on the share and two distinct references here.
    """
    for name in ["eng ", " eng", "eng.", "2026/eng ", "2026/ eng", "2026/eng."]:
        assert is_safe_reference(name) is False, name


def test_is_safe_reference_accepts_nested_paths() -> None:
    """The share is organised in subfolders; the picker browses into them."""
    for name in ["2026/engineering", "2026/Q3/engineering", "a/b/c/d"]:
        assert is_safe_reference(name) is True, name


def test_is_safe_reference_rejects_traversal_and_absolute_paths() -> None:
    invalid_names = [
        "../../etc",
        "..",
        "a/../../etc",
        "a/..",
        "./x",
        ".",
        "/abs",
        "abs/",
        "a//b",
        "",
        " eng",
        "eng ",
        ".hidden",
        "a/.hidden",
    ]
    for name in invalid_names:
        assert is_safe_reference(name) is False, name


def test_is_safe_reference_rejects_backslash_outright() -> None:
    """Not treated as a separator: on Linux it is a legal filename character.

    Accepting `a\\b` would create one directory literally named `a\\b`, which the
    share's Windows clients would read as two.
    """
    for name in ["a\\b", "C:x", "..\\..\\etc", "C:\\Users\\priya"]:
        assert is_safe_reference(name) is False, name


def test_is_safe_reference_rejects_trailing_newline() -> None:
    """`$` matches before a trailing newline; `fullmatch` is what closes it.

    `reference` is UNIQUE and is the folder name, so "eng-2026" and "eng-2026\\n"
    would be two positions that render identically and create two directories.
    """
    for name in ["eng-2026\n", "eng-2026\r", "eng-2026\r\n", "a\nb", "\neng"]:
        assert is_safe_reference(name) is False, name


def test_is_safe_reference_rejects_absurd_depth() -> None:
    assert is_safe_reference("/".join("abcdefgh")) is True
    assert is_safe_reference("/".join("abcdefghi")) is False


def test_folder_for_returns_resolved_path_under_resumes_dir() -> None:
    path = folder_for("Depot Senior Technician")
    assert path.is_relative_to(Path(settings.resumes_dir).resolve())
    assert path.name == "Depot Senior Technician"


def test_folder_for_resolves_nested_references() -> None:
    path = folder_for("2026/engineering")
    base = Path(settings.resumes_dir).resolve()
    assert path == base / "2026" / "engineering"
    assert path.is_relative_to(base)


def test_folder_for_never_escapes_the_root() -> None:
    """The backstop that does not depend on the pattern being exhaustive."""
    for name in ["../../etc", "a/../../etc", "/etc", "..", "a\\b", ""]:
        with pytest.raises(ValueError):
            folder_for(name)
