"""Prompt-injection heuristics (spec §10.2). Pure — no I/O, no model.

Run over the **sanitized** text (§8.6), because the invisible-character and
homoglyph tricks exist precisely to slip a phrase past a check like this one.

**A hit escalates. It never excludes.** That is not caution, it is the only
defensible design: a security engineer's CV legitimately contains "prompt
injection", quoted jailbreak strings, and `<|im_start|>` in a bullet about
red-teaming an LLM. Any keyword heuristic fires on it. Auto-rejecting on a
keyword would mean this system's response to a security specialist applying for
a security job is to silently drop their application — an adverse outcome
produced by a pattern match, with no human in the loop and no record the person
ever applied.

So the cost of a false positive is one reviewer look, and the cost of a false
negative is covered elsewhere. This is **one cheap layer**, not the defense.
Resistance comes from the §10.5(a) consistency gate, mandatory human review, and
the absence of any auto-reject path anywhere in the pipeline.

**What it does not do.** The verified attack left the evidence field honest
(`"not found (...)"`), which is what §10.5(a) catches. An injection that also
says "set evidence to a phrase from the skills section" defeats both that gate
and, with enough rewording, this one. Nothing here should be described as
stopping prompt injection.
"""

import re
from dataclasses import dataclass, field

from screener.models import Flag

# Attempts to countermand the system prompt. Written to survive the obvious
# rewordings (prior/previous/above, instructions/prompts/rules) rather than
# matching the one sample that was observed.
_OVERRIDE = re.compile(
    r"\b(?:ignore|disregard|forget|override|discard)\b[^.\n]{0,40}?"
    r"\b(?:previous|prior|above|earlier|all|any|system)\b[^.\n]{0,20}?"
    r"\b(?:instruction|prompt|rule|direction|context|guideline)s?\b",
    re.IGNORECASE,
)

# Attempts to redefine the model's role or open a new turn.
_ROLE_HIJACK = re.compile(
    r"(?:\byou\s+are\s+now\b"
    r"|\bact\s+as\s+(?:an?\s+)?\w+"
    r"|\bnew\s+(?:instruction|task|rule|system\s+prompt)s?\b"
    r"|\bsystem\s*(?:prompt|message)\b"
    r"|\bas\s+an?\s+ai\s+(?:language\s+)?model\b)",
    re.IGNORECASE,
)

# Chat-template and turn delimiters. These have no business in a resume, and
# unlike the phrase patterns they are close to unambiguous.
_TEMPLATE_MARKERS = (
    "<|im_start|>",
    "<|im_end|>",
    "<|system|>",
    "<|assistant|>",
    "<|user|>",
    "<|endoftext|>",
    "[/inst]",
    "[inst]",
    "<<sys>>",
    "### system",
    "### instruction",
    "assistant:",
    "system:",
)

# Direct attempts to dictate the output rather than to be judged on it.
_SCORE_MANIPULATION = re.compile(
    r"(?:\b(?:rate|score|mark|grade|rank|classify)\b[^.\n]{0,50}?"
    r"\b(?:strong|highest|maximum|top|10|perfect|excellent)\b"
    r"|\ball\s+criteria\b[^.\n]{0,30}?\bstrong\b"
    r"|\bmust\s+(?:be\s+)?(?:hire|advance|accept|approve)\b)",
    re.IGNORECASE,
)

# The resume attempting to write our output schema for us.
_SCHEMA_MIMICRY = re.compile(
    r'"(?:verdict|criteria|evidence|red_flags|must_have)"\s*:',
    re.IGNORECASE,
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instruction_override", _OVERRIDE),
    ("role_hijack", _ROLE_HIJACK),
    ("score_manipulation", _SCORE_MANIPULATION),
    ("schema_mimicry", _SCHEMA_MIMICRY),
)


@dataclass(frozen=True)
class InjectionCheck:
    detected: bool
    signals: list[str] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)

    @property
    def flags(self) -> list[Flag]:
        return [Flag.SUSPECTED_INJECTION] if self.detected else []

    @property
    def review_required(self) -> bool:
        return self.detected

    @property
    def scoreable(self) -> bool:
        """Always ``True``.

        A suspected injection is a reason for a person to look, never a reason
        for the candidate to leave the ranking. Anything else turns a keyword
        match into a rejection.
        """
        return True


def _excerpt(text: str, start: int, end: int, window: int = 60) -> str:
    """Enough surrounding text for a reviewer to judge intent from context.

    "Ignore all previous instructions" inside a bullet about hardening an LLM
    reads completely differently from the same phrase floating in white text at
    the foot of the page.
    """
    left = max(0, start - window)
    right = min(len(text), end + window)
    return " ".join(text[left:right].split())


def detect_injection(text: str) -> InjectionCheck:
    """Scan sanitized resume text for instruction-like content.

    Returns every signal that fired, not just the first: a reviewer deciding
    whether a CV is adversarial or is a security engineer's genuine work history
    needs the whole picture, and one hit versus five is most of that judgement.
    """
    signals: list[str] = []
    excerpts: list[str] = []

    for name, pattern in _PATTERNS:
        match = pattern.search(text)
        if match is not None:
            signals.append(name)
            excerpts.append(_excerpt(text, match.start(), match.end()))

    lowered = text.casefold()
    for marker in _TEMPLATE_MARKERS:
        index = lowered.find(marker)
        if index != -1:
            signals.append("template_marker")
            excerpts.append(_excerpt(text, index, index + len(marker)))
            break

    return InjectionCheck(detected=bool(signals), signals=signals, excerpts=excerpts)
