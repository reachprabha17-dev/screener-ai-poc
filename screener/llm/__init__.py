"""Prompt loading and hashing (spec §9).

Prompts live as files in ``screener/prompts/`` rather than as string literals in
Python, because they are **versioned inputs to a decision**, not code. A prompt
edit changes what the system concludes about a person, so it is hashed and the
hash goes into the run record and the cache key (§6). Change a prompt and every
cached judgment made under the old one stops matching — which is correct, and is
the whole reason the hash exists.

The hash is over the file bytes exactly as loaded. Normalising whitespace before
hashing would let a reformat pass unnoticed, and reformatting a prompt does
change model output.
"""

import hashlib
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


class PromptNotFoundError(FileNotFoundError):
    """A prompt file is missing.

    Fatal by design. Falling back to a default or an empty string would run the
    batch against instructions nobody wrote, and the output would look normal.
    """


@lru_cache(maxsize=8)
def load_prompt(name: str) -> str:
    """Read ``prompts/<name>.md``. Cached — prompts do not change mid-run.

    Caching is not only an optimisation: re-reading per call would let an edit
    land halfway through a batch, so the first half of a run and the second half
    would be judged under different instructions while sharing one
    ``prompt_hash``.
    """
    path = PROMPT_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptNotFoundError(f"prompt not found: {path}") from exc


def prompt_hash(text: str) -> str:
    """Stable identity for a prompt's exact bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_prompt_with_hash(name: str) -> tuple[str, str]:
    prompt = load_prompt(name)
    return prompt, prompt_hash(prompt)
