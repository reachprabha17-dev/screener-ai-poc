"""xberg XML backend safety (spec 8.3 `[assert]`, build gate 20 step 5).

The spec marks this **[assert]** — claimed, not demonstrated — and forbids
treating it as load-bearing until resolved. It is resolved here, by attack rather
than by reading the changelog: DOCX is a zip of XML, and XXE against a resume
parser is a live technique for local file disclosure and SSRF.

Measured on xberg 1.0.12: entity references are **not resolved at all**. Not
external file, not external http, not even internal. The reference is dropped and
the surrounding text survives.

This stays as a test rather than a note in the spec because it is a property of a
pinned dependency, and a version bump is exactly the event that would quietly
undo it. If a future xberg starts resolving entities, this fails on the upgrade
rather than in production.
"""

import asyncio
import re
from pathlib import Path

import pytest
import xberg
from fixtures_files import document_xml, write_docx
from xberg.options import ExtractInput

CANARY = "CANARY_LOCAL_FILE_DISCLOSURE"


def extract_text(path: Path) -> str:
    result = asyncio.run(xberg.extract(ExtractInput(kind="uri", uri=str(path))))
    return " ".join(doc.content or "" for doc in result.results)


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    target = tmp_path / "secret.txt"
    target.write_text(f"{CANARY}\n")
    return target


def test_external_file_entity_is_not_resolved(tmp_path: Path, secret: Path) -> None:
    """The disclosure case: `file://` SYSTEM entity pointing at a local secret.

    On a vulnerable parser the file's contents land in the extracted text, are
    sent to the model as candidate data, and are written verbatim into the trace
    store — a local-file read that exfiltrates itself.
    """
    doctype = f'<!DOCTYPE w:document [<!ENTITY xxe SYSTEM "file://{secret}">]>'
    path = write_docx(
        tmp_path / "xxe.docx",
        document=document_xml("Resume of Asha Nair. Leak: &xxe;", doctype=doctype),
    )

    text = extract_text(path)

    assert CANARY not in text
    assert "Asha Nair" in text  # the document still parsed; only the entity vanished


def test_external_http_entity_is_not_fetched(tmp_path: Path) -> None:
    """The SSRF case. Port 9 (discard) so a fetch attempt fails fast rather than hanging.

    The parser needs no network under any deployment (8.4). This asserts it does
    not want one either — belt and braces with the sandbox, which is what
    actually enforces it under tiers 1 and 2.
    """
    doctype = '<!DOCTYPE w:document [<!ENTITY ext SYSTEM "http://127.0.0.1:9/leak">]>'
    path = write_docx(
        tmp_path / "ssrf.docx",
        document=document_xml("Start &ext; End", doctype=doctype),
    )

    text = extract_text(path)

    assert "Start" in text
    assert "End" in text


def test_internal_entities_are_not_expanded(tmp_path: Path) -> None:
    """Even a benign internal entity is dropped.

    Worth pinning: it is the mechanism that makes the billion-laughs case below
    impossible, and it is also the reason a legitimate DOCX using entities would
    lose that text. No writer produces one, but the behaviour should be recorded
    rather than discovered.
    """
    doctype = '<!DOCTYPE w:document [<!ENTITY greet "HELLO_INTERNAL">]>'
    path = write_docx(
        tmp_path / "internal.docx",
        document=document_xml("Start &greet; End", doctype=doctype),
    )

    text = extract_text(path)

    assert "HELLO_INTERNAL" not in text
    assert "Start" in text


def test_billion_laughs_does_not_expand(tmp_path: Path) -> None:
    """Quadratic entity expansion: memory exhaustion inside the parser.

    Contained twice over — no expansion here, and the sandbox's RLIMIT_AS if a
    future version ever does expand.
    """
    doctype = (
        "<!DOCTYPE w:document ["
        '<!ENTITY a "' + "a" * 50 + '">'
        '<!ENTITY b "' + "&a;" * 10 + '">'
        '<!ENTITY c "' + "&b;" * 10 + '">'
        '<!ENTITY d "' + "&c;" * 10 + '">'
        '<!ENTITY e "' + "&d;" * 10 + '">'
        '<!ENTITY f "' + "&e;" * 10 + '">'
        "]>"
    )
    path = write_docx(
        tmp_path / "bomb.docx",
        document=document_xml("&f;", doctype=doctype),
    )

    text = extract_text(path)

    assert len(text) < 1000
    assert not re.search(r"a{200,}", text)
