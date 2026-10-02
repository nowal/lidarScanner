"""A last look at a reply for a dropped or doubled word.

Quintin, Oct 1: "I've walked through what you available, two spaces
totaling around 850 square feet" -- an opener every homeowner on that scan
would have read, missing its verb. A prompt rule cannot promise this never
happens; a second read can catch it.

The second read is a model call, so it is boxed in three ways. It runs
only where configured (``LIDARAI_PROOFREAD_SCOPE``: openers by default,
which is one call per thread). It has a short timeout and no retries, and
any failure returns the text untouched. And its answer is only used when
``accept`` agrees it is the original with a few small grammatical repairs:
it may add or swap in function words and fix an inflection or a doubled
word, and nothing else, so it cannot add a claim, a number or a name.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

SYSTEM = (
    "You proofread one short message that an assistant is about to send to a "
    "homeowner. The message is between <message> tags. Fix only clear "
    "grammatical slips: a missing word, a doubled word, a wrong verb form, a "
    "missing article. Change nothing else: not the wording, the meaning, the "
    "facts, the numbers, the names, the punctuation or the line breaks. If the "
    "message is already correct, return it exactly as given. Return only the "
    "message text, without the tags, with no preamble and no explanation."
)

# The only words a correction may add or swap in.
_FUNCTION_WORDS = frozenset(
    "a an the have has had having is are was were be been being am do does did "
    "to of in on at for with from by as that this these those it its it's i "
    "i've i'm you you've your you're we we've our they their there and or but "
    "so if than then".split()
)
_TOKEN = re.compile(r"[A-Za-z0-9$%'’-]+|[^\sA-Za-z0-9]")
_MIN_CHARS, _MAX_CHARS = 20, 1500


def _norm(token: str) -> str:
    return token.lower().replace("’", "'")


def _punct(token: str) -> bool:
    return bool(re.fullmatch(r"[^\w\s]", token))


def _inflection(token: str, others: list[str]) -> bool:
    """walk/walked, total/totaling: the same word in another form."""
    t = _norm(token)
    if len(t) < 3 or any(ch.isdigit() for ch in t):
        return False
    # A shared prefix is not an inflection: paint -> painful, for example.
    # Keep this deliberately narrow; a missed repair leaves the original.
    def forms(word: str) -> set[str]:
        return {word + "s", word + "ed", word + "ing"}

    return any(t in forms(_norm(o)) or _norm(o) in forms(t) for o in others)


def accept(original: str, edited: str, *, max_edits: int = 4) -> bool:
    """Is ``edited`` the original with only small grammatical repairs?"""
    if not edited or original.count("\n") != edited.count("\n"):
        return False
    a, b = _TOKEN.findall(original), _TOKEN.findall(edited)
    la, lb = [_norm(t) for t in a], [_norm(t) for t in b]
    # A grammatical repair must not reverse a claim or change a promise.
    # Check before doubled-word removal, which could otherwise erase "no".
    protected = {"not", "no", "never", "will", "would", "can", "could", "should", "may", "might", "must"}
    def claims(tokens: list[str]) -> list[str]:
        return [t for t in tokens if t in protected or t.endswith("n't")]
    if claims(la) != claims(lb):
        return False
    if la == lb:
        return False  # nothing to apply
    edits = 0
    matcher = difflib.SequenceMatcher(a=la, b=lb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed, added = a[i1:i2], b[j1:j2]
        edits += max(len(removed), len(added))
        for token in added:
            if _norm(token) in _FUNCTION_WORDS or _punct(token) or _inflection(token, removed):
                continue
            return False
        for offset, token in enumerate(removed):
            if _norm(token) in _FUNCTION_WORDS or _punct(token) or _inflection(token, added):
                continue
            at = i1 + offset
            if at > 0 and la[at - 1] == la[at]:
                continue  # a doubled word
            return False
    return 0 < edits <= max_edits


async def _call_model(text: str) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=float(settings.proofread_timeout_seconds),
        max_retries=0,
    )
    response = await client.messages.create(
        model=settings.proofread_model.strip() or settings.anthropic_model,
        max_tokens=settings.anthropic_max_tokens,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"<message>\n{text}\n</message>"}],
        output_config={"effort": "low"},
    )
    # A refusal or a cut-off answer is not a proofread message.
    if response.stop_reason != "end_turn":
        return ""
    return "".join(block.text for block in response.content if block.type == "text")


def enabled_for(*, opening: bool) -> bool:
    scope = (settings.proofread_scope or "").strip().lower()
    if scope == "all":
        return True
    return scope == "opening" and opening


async def proofread(text: str, *, opening: bool, caller: Any = None) -> str:
    """The text, with a dropped or doubled word repaired when a second read
    finds one. Never raises, never returns anything but the original or a
    version ``accept`` approved."""
    if not text or not enabled_for(opening=opening) or not settings.anthropic_api_key:
        return text
    if not _MIN_CHARS <= len(text) <= _MAX_CHARS:
        return text
    invoke = caller or _call_model
    try:
        raw = await asyncio.wait_for(invoke(text), timeout=float(settings.proofread_timeout_seconds))
    except Exception as exc:  # noqa: BLE001 -- the reply goes out as written
        logger.info("Proofread skipped: %s", type(exc).__name__)
        return text
    edited = re.sub(r"^\s*<message>\s*|\s*</message>\s*$", "", raw or "").strip()
    if not accept(text.strip(), edited):
        return text
    logger.info("Proofread repaired a reply (%d -> %d chars)", len(text), len(edited))
    return edited
