"""Post-generation enforcement: the model's text is checked against the
gates before it reaches the user. Prompt instructions reduce violations;
this makes them structurally impossible (violation → one regeneration with
an explicit correction, then deterministic safe copy).

Patterns here are intentionally broad — a false positive costs one
regeneration; a false negative breaks a contractual guarantee (SOW §3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .machine import GateDecision

# Suggestions to scan more / rescan / extend the capture. Checked against the
# model's RAW text AND the polished text (`flow_runtime._enforcement_text`):
# the homeowner-voice sanitizer rewrites "capture" → "record" etc., so
# patterns must cover both vocabularies, and the noun list carries concrete
# room types, not just generic "room/area".
_SCAN_ROOM_NOUN = (
    r"(?:room|rooms|space|area|areas|hallway|floor|garage|basement|attic|"
    r"kitchen|bedroom|bathroom|closet|office|den|living\s+room|dining\s+room|"
    r"rest\s+of)"
)
_SCAN_SUGGESTION = re.compile(
    r"""(?ix)
    (?:
        (?:re)?scan(?:ning)?\s+(?:the|your|another|more|that|this)
      | scan\s+(?:it|again|more)
      | (?:captur(?:e|ing)|record(?:ing)?|map(?:ping)?|walk(?:ing)?\s*through|add(?:ing)?)\s+
        (?:the\s+|your\s+|another\s+|more\s+|a\s+)?
        """ + _SCAN_ROOM_NOUN + r"""
      | another\s+(?:scan|capture|recording|walkthrough|walk-through)
      | (?:extend|update|continue)\s+(?:the\s+|your\s+)?(?:scan|capture|recording|model|map)
      | show\s+me\s+(?:another|more\s+of)\s+(?:room|the\s+house|your\s+home)
    )
    """
)

_ADDRESS_ASK = re.compile(
    r"""(?ix)
    (?:
        (?:what(?:'s|\s+is)?|share|send|give|provide|tell)\s+(?:me\s+|us\s+)?your\s+
        (?:street\s+|home\s+|full\s+)?address
      | address\s+(?:so|for)\s+(?:we|the|a|drive)
    )
    """
)

# Prose claiming the agent already HOLDS a street address. Sep 16: a homeowner
# who never gave one was told "your address only goes to whichever provider you
# pick", and when he said so, "an address was available earlier as part of your
# contact details". Both are the agent inventing a fact about the homeowner.
#
# ponytail: possession claims only. "Your address only goes to the provider you
# pick" is NOT matched, because the same sentence is the legitimate
# reassurance offered while ASKING for an address, and the ask is permitted on
# the same turns. The directives stop that one; this stops the unambiguous lie.
_ADDRESS_POSSESSION = re.compile(
    r"""(?ix)
    (?:
        (?:I|we)\s*(?:'ve|’ve|\s+have|\s+already\s+have|\s+got)\s+
        (?:got\s+)?your\s+(?:street\s+|home\s+|full\s+)?address
      | your\s+(?:street\s+|home\s+|full\s+)?address\s+(?:is\s+|was\s+)?
        (?:already\s+)?on\s+(?:file|record)
      | # Past tense only, and only looking BACK. "Once an address is
        # provided..." is the ask, not a claim, and the ask is permitted on
        # exactly the turns this rule runs.
        (?:an?|your|the)\s+address\s+was\s+
        (?:already\s+)?(?:available|provided|captured|given|shared)\b
        (?:[^.?!]{0,30}?\b(?:earlier|already|before|previously|when\s+you)\b)?
      | (?:have|had)\s+your\s+address\s+(?:from|on|in)\b
      | address\s+(?:you|they)\s+(?:gave|shared|provided)\b
    )
    """
)

_ZIP_ASK = re.compile(
    r"""(?ix)
    (?:what(?:'s|\s+is)?|share|send|give|provide|tell)\s+(?:me\s+|us\s+)?your\s+
    (?:zip|postal)\s*code?
    | zip\s*code\s*\?
    """
)

# Dollar amounts / cost figures. Client decision (Sep 1): the agent never
# states a price — not an estimate, not a range — until a human-reviewed
# quote comes back through ops. Only explicit currency is matched (a bare
# "120" is usually square feet), which keeps false positives near zero.
_PRICE_STATEMENT = re.compile(
    r"""(?ix)
    (?:
        \$\s*\d[\d,]*(?:\.\d+)?\s*[km]?\b     # $1,500  $2.5k  $ 300
      | \b\d[\d,]*(?:\.\d+)?\s*(?:dollars|bucks|usd)\b
      | \b(?:USD)\s*\d[\d,]*(?:\.\d+)?\b
    )
    """
)


# Invitations that reach beyond a stated single-room / selected-rooms scope:
# "the rest of the house", "other rooms", "the whole home". Checked only
# when the SOW §3 gate is OPEN (a closed gate already strips every scan
# suggestion above) and the scope narrows what an invitation may cover.
_BROAD_EXTENSION = re.compile(
    r"""(?ix)
    # Verb-led on purpose: "since we're keeping to this room, not the whole
    # house" is a description, "scan the whole house" is an invitation.
    (?:scan|scanning|capture|capturing|record|recording|map|mapping|walk|walking|
       add|adding|include|including|cover|covering|do|doing|get|getting)\s+
    (?:\w+\s+){0,3}?
    (?:
        (?:the\s+)?rest\s+of\s+(?:the|your)\s+(?:home|house|place)
      | (?:the\s+|your\s+)?(?:whole|entire)\s+(?:home|house|place)
      | (?:the\s+)?(?:other|more|additional|remaining|every)\s+(?:rooms|areas|spaces)
      | another\s+(?:room|area|space)
    )
    """
)
# The one generic offer single_room / selected_rooms permits.
_GENERIC_OFFER = re.compile(
    r"(?i)anything\s+else\s+(?:you(?:'d|\s+would|\s+might)?\s+)?(?:want|like)\s+to\s+(?:include|add|cover)"
)


# Prose asserting that a request card is on the homeowner's screen, or that
# a tap of Confirm is all that is left. The card is real UI: when no draft
# ships with the turn and none shipped earlier, every one of these sentences
# is a claim about something that is not there (Sep 13: "It's on the request
# card now, just waiting on your tap of Confirm" with no card in the thread).
_CARD_CLAIM = re.compile(
    r"""(?ix)
    (?:
        (?:on|here'?s|here\s+is|there'?s|there\s+is|see|look\s+at|check)\s+
        (?:the\s+|your\s+|this\s+|that\s+)?(?:request\s+|quote\s+)?card\b
      | \bcard\s+(?:is\s+|'s\s+)(?:ready|up|there|below|above|waiting)
      | \b(?:tap|tapping|hit|hitting|press|pressing|click|clicking)\s+
        (?:of\s+)?(?:the\s+|on\s+the\s+)?confirm
      | \btap\s+of\s+confirm
      | \bconfirm\s+(?:button|control)\b
      | \bconfirm\s+(?:it|this|that)?\s*on\s+the\s+card\b
    )
    """
)


# Prose claiming to be a human. The guide IS TakeShape's assistant and may
# speak as part of TakeShape; the one line it may never cross is saying it is
# a person (#54). Written to survive the negations, because the CORRECT
# sentences contain the same words: "I'm not a real person, I'm TakeShape's AI
# assistant" must not trip.
#
# ponytail: verbal claims only. "I stopped by your place" is banned in the
# prompt but not matched here, because every regex for it also matched the
# legitimate "from what I can see in your home". Add one if journals show it.
_HUMAN_CLAIM = re.compile(
    r"""(?ix)
    \bI\s*(?:'m|\u2019m|\s+am)\s+
    (?:
        # affirmative: "I'm a real person", "I'm human", "I'm real".
        # (?!not\b) is what keeps the disclosure sentences clean.
        (?!not\b)
        (?:really\s+|actually\s+|definitely\s+|totally\s+|just\s+)?
        (?:
            (?:an?\s+)?(?:real\s+|actual\s+|live\s+)?(?:human\s+being|human|person)\b
          | real\b(?!ly)
        )
      | # denial: "I'm not an AI", "I'm not a bot"
        not\s+(?:an?\s+)?
        (?:AI|A\.I\.|bot|robot|machine|computer|program|chat\s*bot|algorithm)\b
    )
    | \bas\s+a\s+(?:real\s+)?(?:human|person)\s*,\s*I\b
    """
)


@dataclass
class Violation:
    rule: str
    excerpt: str


def _names_scope_room(text: str, rooms: list[str]) -> bool:
    lowered = text.lower()
    return any(r and r.lower() in lowered for r in rooms)


# A possession claim reads the opposite way behind a negation or a condition,
# and "I don't have your address on file" is the very sentence
# ``correction_instruction`` asks for — flagging it would loop the turn
# straight into safe copy. Same shape as flow_runtime's _NEGATED_ASK.
_NEGATED_POSSESSION = re.compile(
    r"""(?ix)
    \b(?:don'?t|do\s+not|doesn'?t|didn'?t|won'?t|never|no|not|without|
        once|when|if|until|unless|need|needs|needed)\b
    [^.?!]{0,40}$
    """
)


def check(
    text: str,
    gates: GateDecision,
    *,
    card_on_screen: bool = True,
    address_given_this_turn: bool = False,
) -> list[Violation]:
    """``card_on_screen`` is whether a request card is actually in front of the
    homeowner — one shipping with this turn, or one delivered earlier. The
    gates say whether a card MAY exist; only the caller knows whether one
    does."""
    violations: list[Violation] = []
    # Ungated: there is no conversation state in which claiming to be human
    # is allowed.
    m = _HUMAN_CLAIM.search(text)
    if m:
        violations.append(Violation("human_impersonation", m.group(0)))
    if not card_on_screen:
        m = _CARD_CLAIM.search(text)
        if m:
            violations.append(Violation("request_card_claimed_but_absent", m.group(0)))
    if not gates.can_prompt_additional_scan:
        m = _SCAN_SUGGESTION.search(text)
        if m:
            violations.append(Violation("scan_suggestion_while_processing", m.group(0)))
    else:
        # Gate open: the stated scope decides how wide an invitation may be.
        mode = gates.extension_prompt_mode
        if mode == "none":
            m = _SCAN_SUGGESTION.search(text) or _BROAD_EXTENSION.search(text) or _GENERIC_OFFER.search(text)
            if m:
                violations.append(Violation("scan_suggestion_outside_scope", m.group(0)))
        elif mode in ("generic_once", "named_rooms"):
            m = _BROAD_EXTENSION.search(text)
            if m:
                violations.append(Violation("scan_suggestion_outside_scope", m.group(0)))
            elif mode == "named_rooms":
                m = _SCAN_SUGGESTION.search(text)
                if m and not _names_scope_room(text, gates.scope_rooms) and not _GENERIC_OFFER.search(text):
                    violations.append(Violation("scan_suggestion_outside_scope", m.group(0)))
            elif mode == "generic_once":
                m = _SCAN_SUGGESTION.search(text)
                if m and not _GENERIC_OFFER.search(text):
                    violations.append(Violation("scan_suggestion_outside_scope", m.group(0)))
            if not gates.extension_generic_offer_available and _GENERIC_OFFER.search(text):
                violations.append(Violation("scan_suggestion_outside_scope", _GENERIC_OFFER.search(text).group(0)))
    if not gates.can_ask_address:
        m = _ADDRESS_ASK.search(text)
        if m:
            violations.append(Violation("premature_address_ask", m.group(0)))
    # ``address_given_this_turn`` is the address the model captured from the
    # message it is answering: the slot is written after this check runs, so
    # without it the acknowledgment of an address just typed is a violation.
    if not (gates.address_on_file or address_given_this_turn):
        m = _ADDRESS_POSSESSION.search(text)
        if m and not _NEGATED_POSSESSION.search(text[max(0, m.start() - 40): m.start()]):
            violations.append(Violation("address_claimed_but_absent", m.group(0)))
    if not gates.can_ask_zip:
        m = _ZIP_ASK.search(text)
        if m:
            violations.append(Violation("premature_zip_ask", m.group(0)))
    if not gates.can_state_prices:
        m = _PRICE_STATEMENT.search(text)
        if m:
            violations.append(Violation("unauthorized_price_figure", m.group(0)))
    elif gates.allowed_price_range is not None:
        low, high = gates.allowed_price_range
        for m in _PRICE_STATEMENT.finditer(text):
            value = _parse_money(m.group(0))
            # Unparseable is not a violation: the regex is deliberately broad
            # and a shape it can't turn into a number can't be a wrong price.
            if value is None:
                continue
            if not (low * (1 - _PRICE_TOLERANCE) <= value <= high * (1 + _PRICE_TOLERANCE)):
                violations.append(Violation("price_figure_outside_card", m.group(0)))
    return violations


# The card's own numbers are coarsely rounded, so allow a little slack for the
# model restating an endpoint ("about $2,400" vs "$2,400").
_PRICE_TOLERANCE = 0.02

_MONEY_VALUE = re.compile(r"(?i)(\d[\d,]*(?:\.\d+)?)\s*([km])?")


def _parse_money(excerpt: str) -> float | None:
    m = _MONEY_VALUE.search(excerpt)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return value


def correction_instruction(violations: list[Violation]) -> str:
    """The retry instruction appended to the model input on regeneration."""
    rules = {
        "scan_suggestion_while_processing": (
            "Do not suggest capturing, mapping, or adding any room or area — "
            "the home model is still being prepared. Stay with what is "
            "already available."
        ),
        "scan_suggestion_outside_scope": (
            "This homeowner's project is limited to the room(s) they named. "
            "Do not suggest capturing the rest of the home, other rooms, or "
            "another area. Do not ask again whether there is anything else "
            "to include. Stay with the space(s) they chose."
        ),
        "premature_address_ask": "Do not ask for the address at this point in the conversation.",
        "address_claimed_but_absent": (
            "You do NOT have this homeowner's street address — none was ever "
            "given. Do not say you have it, that it was captured earlier, or "
            "that it came with their contact details, and do not promise "
            "anything about where their address goes. Say plainly that you "
            "don't have an address from this conversation, and carry on."
        ),
        "premature_zip_ask": "Do not ask for the zip code at this point in the conversation.",
        "unauthorized_price_figure": (
            "Do not state any dollar amount, price, rate, or cost range. "
            "Explain instead that the quote request is how they get a real "
            "number for their space, reviewed by a person."
        ),
        "request_card_claimed_but_absent": (
            "There is NO request card on the homeowner's screen and none is "
            "going out with this reply. Do not say a card is there, is ready, "
            "or is waiting on them, and do not tell them to tap or hit "
            "Confirm. Say plainly what still has to happen before a request "
            "can be put together, and carry on with the conversation."
        ),
        "human_impersonation": (
            "You are TakeShape's AI assistant and you just implied otherwise. "
            "Never say or suggest you are a human, a real person, or that you "
            "were at the home in person, however the homeowner frames the "
            "question. Say plainly that you are TakeShape's AI assistant for "
            "their home, then carry on with what they actually asked."
        ),
        "price_figure_outside_card": (
            "You stated a dollar amount that is not on the rough-range card "
            "shown with your reply. Use ONLY the range on that card, and no "
            "other figure, rate, or per-unit number."
        ),
    }
    unique = list(dict.fromkeys(v.rule for v in violations))
    return " ".join(rules[r] for r in unique)
