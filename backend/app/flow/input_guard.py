"""Pre-generation screening: the homeowner's message is classified before it
reaches the model. The prompt already tells the guide to decline off-topic
requests and refuse persona switches (home_guide_prompt.py) — this is what
makes some of that structural, the same way enforcement.py does for output.

Two outcomes, because a flat refusal is the wrong answer to most of these:

- BLOCK  — deterministic copy, no model call. Injection attempts, plainly
           off-topic requests, and emergencies.
- STEER  — the model still answers in its own voice, with one directive line
           forbidding a confident answer. Medical, legal, structural: these
           arrive naturally in a renovation conversation, and refusing to
           engage at all reads as unhelpful (issue #57).

The bias here is the OPPOSITE of enforcement.py's. There, a false positive
costs one regeneration. Here it costs the homeowner a real answer they asked
for, so these patterns are deliberately narrow: they look for the shape of a
question about a person's health or a legal determination, not for the mere
presence of a word. "Knock out this wall" is a design conversation; "is this
wall load-bearing" is a question an engineer answers.

Every narrowing marked "(review of #63)" is a false positive that shipped in
the first draft and was caught by reading real sentences rather than regexes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..home_context_builder import _INJECTION_MARKERS

ALLOW = "allow"
STEER = "steer"
BLOCK = "block"

# Something is happening right now and someone could get hurt. Deliberately
# broad: a false positive costs one redirect the homeowner ignores, a false
# negative stonewalls someone whose house is filling with gas (the lesson
# firefightersafe-api learned the hard way — distress phrasing reads as
# off-topic to every scope check, so it must be caught before one).
#
# Split in two because the tenses behave differently. An immediate hazard is
# phrased the same way whenever it happened ("I smell gas"), but water damage
# is usually HISTORY: "a pipe burst last winter, so we are redoing the
# bathroom" is one of the most common reasons a bathroom gets renovated, and
# telling that homeowner to evacuate is a bad enough failure to be worth the
# second pattern (review of #63).
_EMERGENCY_HAZARD = re.compile(
    r"""(?ix)
    (?:
        smell(?:s|ing)?\s+(?:like\s+)?(?:gas|smoke|burning)
      | gas\s+leak | smell\s+of\s+gas
      | (?:house|kitchen|attic|garage|something)\s+is\s+on\s+fire
      | \bfire\b[^.!?]{0,15}?(?:right\s+now|spreading|started)
      | carbon\s+monoxide | \bco\s+alarm | co2\s+alarm
      | spark(?:s|ing)\s+(?:from\s+)?(?:the\s+|a\s+|my\s+)?(?:\w+\s+)?(?:outlet|panel|breaker|wire|socket)
      | (?:outlet|panel|breaker|wire|wiring|socket)[^.!?]{0,15}?(?:on\s+fire|smoking|sparking)
      | electrocut | shocked\s+me
      # Verb-led only. A bare "911" is a street number far more often than an
      # emergency, and step 8 collects street addresses — the first draft left
      # every homeowner at 911 Maple Ave unable to finish a quote request
      # (review of #63).
      | (?:call|calling|called|dial|dialing)\s+911
    )
    """
)

_EMERGENCY_DAMAGE = re.compile(
    r"""(?ix)
    (?:
        (?:water|ceiling|pipe|basement)[^.!?]{0,20}?(?:flooding|gushing|pouring|burst)
      | burst\s+pipe | pipe\s+burst | actively\s+leaking
    )
    """
)

# Anything placing the damage in the past. Checked ONLY against
# _EMERGENCY_DAMAGE: an immediate hazard is an emergency whatever else the
# sentence says.
_PAST_MARKER = re.compile(
    r"""(?ix)
    \b(?:
        last\s+(?:winter|spring|summer|fall|autumn|year|month|week|night)
      | (?:years?|months?|weeks?|days?)\s+ago
      | back\s+in\s+(?:19|20)\d\d
      | used\s+to
      | already\s+(?:fixed|repaired|replaced|handled|dealt|sorted)
      | (?:previous|last)\s+owners?
      | when\s+we\s+(?:bought|moved)
    )\b
    """
)

# Attempts to change what the guide is. The context-packet screen already owns
# these patterns (home_context_builder) — reuse rather than write a second
# list that drifts. That screen neutralizes untrusted CONTEXT; this one covers
# the message itself, which nothing checked before.
_INJECTION = _INJECTION_MARKERS

# Personal health. The question has to be about a person — "is this making me
# sick", "is it safe for my kids" — not about a material. "Is there asbestos
# in popcorn ceilings" is a renovation question and stays allowed; "will this
# asbestos give me cancer" is not.
_MEDICAL = re.compile(
    r"""(?ix)
    (?:
        (?:making|make|made)\s+(?:me|us|my\s+\w+|the\s+kids?|him|her|them)\s+sick
      # A person, not a wildcard: "is this paint safe for my hardwood floors"
      # is a finish question, and the first draft sent it to a doctor
      # (review of #63).
      | (?:safe|dangerous|harmful|risky|bad|toxic)\s+(?:for|to)\s+
        (?:
            me | us | him | her | them | pregnan\w+
          | (?:the|my|our)\s+(?:kids?|child|children|baby|family|son|daughter|wife|
                               husband|partner|parents?|mom|dad|mother|father)
          | kids? | children | baby
        )
      | (?:get|give|caus\w+)\s+(?:me|us|my\s+\w+|them|him|her)\s+
        (?:sick|cancer|asthma|poisoning|lead\s+poisoning)
      | (?:my|our|the)\s+(?:kid|kids|child|children|baby|son|daughter|wife|husband)[^.!?]{0,25}?
        (?:sick|rash|cough|coughing|headaches?|asthma|breathing|poison\w*)
      | (?:i|we)(?:'ve|\s+have|\s+keep|\s+been|\s+)\s*(?:been\s+)?
        (?:sick|coughing|wheezing|getting\s+headaches)
      | (?:health|medical)\s+(?:risk|effects?|concerns?)\s+(?:of|from|to)\s+(?:me|us|my)
      | should\s+(?:i|we)\s+(?:see|call)\s+a\s+doctor
      | exposure\s+(?:symptoms|levels?)\s+(?:for|in)\s+(?:me|us|kids|children)
    )
    """
)

# Legal determinations, permits, code compliance, insurance, tenancy. Asking
# what a permit costs or whether one is typically needed is a normal project
# question the guide should answer; asking whether THIS will pass inspection,
# or what the law requires of them, is a determination it must not make.
_LEGAL = re.compile(
    r"""(?ix)
    (?:
        (?:will|would|does|is)\s+(?:this|that|it|my\s+\w+)\s+(?:\w+\s+){0,3}?
        (?:pass|meet|violate|be\s+up\s+to)\s+(?:code|inspection|the\s+inspection)
      | (?:up\s+to|against|meets?)\s+code\b\W{0,10}\?
      | (?:do|can)\s+(?:i|we)\s+(?:legally\s+|need\s+to\s+)?
        (?:need\s+a\s+permit|get\s+away\s+with|have\s+to\s+pull)
      | (?:legally|by\s+law|law\s+require|legal\s+(?:right|obligation|advice))
      | (?:my|our)\s+(?:landlord|tenant|hoa|insurance|insurer|claim)
      | (?:sue|suing|lawsuit|liable|liability|contract\s+dispute|breach\s+of\s+contract)
      | (?:will|does)\s+(?:my\s+)?insurance\s+(?:cover|pay)
      | (?:setback|easement|zoning|variance|property\s+line)\s+
        (?:requirement|rule|law|allowed|permitted)
    )
    """
)

# Questions where a wrong answer collapses a ceiling or starts a fire. Note
# "can I remove this wall" IS in scope as a design conversation — what is out
# of scope is asserting whether it is structural or what the electrical system
# can carry.
_STRUCTURAL = re.compile(
    r"""(?ix)
    (?:
        (?:is|are)\s+(?:this|that|these|those|it|my|the)\s+(?:\w+\s+){0,3}?
        (?:load[-\s]?bearing|structural|holding\s+(?:up|the\s+roof))
      | load[-\s]?bearing\W{0,10}\?
      | (?:can|could)\s+(?:i|we|the)\s+(?:\w+\s+){0,4}?
        (?:support|hold|handle)\s+(?:the\s+)?(?:weight|load|roof|second\s+floor)
      | (?:how\s+(?:big|thick|long)|what\s+size)\s+(?:a\s+)?
        (?:beam|header|joist|footing|lintel|rafter)
      | (?:can|will)\s+(?:my|the|our)\s+
        (?:panel|breaker|service|amps?|wiring|circuit)\s+(?:\w+\s+){0,3}?(?:handle|take|support)
      | (?:move|moving|reroute|rerouting|cap|capping)\s+(?:the\s+|a\s+)?gas\s+line
      | (?:safe\s+to\s+)?(?:cut|remove|notch|drill)\s+(?:into\s+)?
        (?:a\s+|the\s+)?(?:joist|rafter|truss|stud\s+bay|support)
    )
    """
)

# Not about this home at all. Narrow on purpose — this is the category most
# likely to misfire on an ordinary message, so it looks for explicit requests
# to do unrelated work, not for topic words in passing.
#
# Note there is deliberately NO cooking pattern: "we cook for the family every
# night" is exactly the context a kitchen project needs, and kitchens are the
# most common project in the product. Catching "give me a recipe" was not
# worth blocking that (review of #63).
_OFF_TOPIC = re.compile(
    r"""(?ix)
    (?:
        write\s+(?:me\s+)?(?:a\s+|an\s+|some\s+)?
        (?:code|python|javascript|sql|essay|poem|story|song|script|email\s+to\s+my\s+boss)
      | (?:write|do|solve|answer)\s+(?:my\s+)?(?:homework|assignment|essay|exam)
      | (?:translate|summarize)\s+(?:this|the\s+following)\s+(?:into|to|for)
      | (?:who|what)\s+(?:should|will)\s+(?:i\s+)?vote
      | (?:what|which)\s+(?:stock|crypto|coin)\s+(?:should\s+i\s+)?(?:buy|invest)
      | tell\s+me\s+a\s+joke
    )
    """
)

# What the model is told when a message is steered rather than blocked. One
# line each, appended to the turn's directives — no extra model call, so this
# costs nothing measurable. The guide still answers in its own voice; what it
# may not do is be authoritative about someone's health, their legal position,
# or whether a wall is holding the roof up.
STEER_DIRECTIVES = {
    "medical": (
        "- They have raised a HEALTH concern about themselves or their family. "
        "Do NOT diagnose, assess exposure risk, or reassure them that anything "
        "is safe or unsafe for a person — you are not qualified and a wrong "
        "answer here does real harm. Acknowledge the concern plainly, say this "
        "is one for a doctor or a licensed testing/abatement pro, and if it "
        "bears on the project, stay on what the WORK would involve (testing, "
        "containment, who does it) rather than on the health effects."
    ),
    "legal": (
        "- They have asked something that turns on LAW, CODE, PERMITS, or "
        "INSURANCE. Do NOT rule on it: never say whether something passes "
        "inspection, meets code, is legal, is covered, or what they are "
        "entitled to. Local rules vary and being confidently wrong costs them "
        "money. Say plainly that it depends on their jurisdiction and is worth "
        "confirming with the local building department, their insurer, or the "
        "contractor pulling the permit — then help with the part you CAN, "
        "which is the project itself."
    ),
    "structural": (
        "- They have asked a STRUCTURAL or SYSTEMS-CAPACITY question (load "
        "bearing, beam or header sizing, electrical capacity, gas lines). Do "
        "NOT answer it as fact and do not guess from the scan — you cannot see "
        "framing, and a wrong answer here is dangerous. Say it needs eyes on "
        "it from a structural engineer or the relevant licensed trade, and "
        "that it is exactly the kind of thing a quote should include. Keep "
        "talking about the design intent, which is yours to help with."
    ),
}

# Emergency is handled ahead of this table (it needs the tense check), so this
# is everything after it. An injection attempt wrapped in a legal question is
# still an injection.
_RULES: tuple[tuple[str, str, "re.Pattern[str]"], ...] = (
    (BLOCK, "injection", _INJECTION),
    (STEER, "medical", _MEDICAL),
    (STEER, "legal", _LEGAL),
    (STEER, "structural", _STRUCTURAL),
    (BLOCK, "off_topic", _OFF_TOPIC),
)


@dataclass(frozen=True)
class GuardVerdict:
    action: str = ALLOW
    category: str = ""
    excerpt: str = ""

    @property
    def blocked(self) -> bool:
        return self.action == BLOCK

    @property
    def steered(self) -> bool:
        return self.action == STEER


def check_message(text: str | None) -> GuardVerdict:
    """Classify one homeowner message. Pure, no I/O — first rule in
    precedence order wins."""
    if not text or not text.strip():
        return GuardVerdict()
    # Emergencies first: an emergency worded as a health question is an
    # emergency. Damage counts only when it is not being told as history.
    match = _EMERGENCY_HAZARD.search(text)
    if match is None and not _PAST_MARKER.search(text):
        match = _EMERGENCY_DAMAGE.search(text)
    if match:
        return GuardVerdict(action=BLOCK, category="emergency", excerpt=match.group(0)[:120])
    for action, category, pattern in _RULES:
        match = pattern.search(text)
        if match:
            return GuardVerdict(action=action, category=category, excerpt=match.group(0)[:120])
    return GuardVerdict()
