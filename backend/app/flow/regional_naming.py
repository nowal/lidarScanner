"""Room vocabulary varies by region, and the homeowner's word is the right one.

Quintin, Sep 11: the index named a space a "sitting area" and he reads that
room as something else; Chance's point on the same call was that regional
terminology is ground truth, not a mistake to correct. We already ask for a
zip in step 3, so the region is known by the time most room talk happens.

Two things come out of that, and they are deliberately different in kind:

* ``SYNONYMS`` are nationally safe aliases -- a "front room" is a living room
  wherever it is said -- so they go into resolution for every home and do not
  depend on the zip at all. Getting one wrong sends the agent to the wrong
  room, so this list stays conservative.
* ``directive_for`` only tells the model which words are *likely* to come up
  near that zip and to mirror whatever the homeowner actually says. It never
  renames a room and never contradicts them. A guess about dialect is cheap
  when it only shapes vocabulary and expensive when it picks a room.

The zip-to-region map is the standard first-digit grouping of states, which
is exact for the digit and approximate for the dialect -- good enough for
"these words may come up", which is all it is used for.
"""

from __future__ import annotations

# First ZIP digit -> the states it covers -> census region.
#   0 CT MA ME NH NJ PR RI VT   1 DE NY PA          2 DC MD NC SC VA WV
#   3 AL FL GA MS TN            4 IN KY MI OH       5 IA MN MT ND SD WI
#   6 IL KS MO NE               7 AR LA OK TX       8 AZ CO ID NM NV UT WY
#   9 AK CA HI OR WA
_REGION_BY_FIRST_DIGIT = {
    "0": "northeast",
    "1": "northeast",
    "2": "south",
    "3": "south",
    "4": "midwest",
    "5": "midwest",
    "6": "midwest",
    "7": "south",
    "8": "west",
    "9": "west",
}

# Aliases that mean the same room anywhere in the country. Used for
# resolution, so every entry here must be one a homeowner could not
# reasonably mean another way.
SYNONYMS = {
    "front room": "living room",
    "great room": "living room",
    "sitting room": "living room",
    "rec room": "living room",
    "parlor": "living room",
    "parlour": "living room",
    "keeping room": "living room",
    "breakfast nook": "dining room",
    "breakfast room": "dining room",
    "mud room": "mudroom",
    "drop zone": "mudroom",
    "lower level": "basement",
    "cellar": "basement",
    "primary bath": "primary bathroom",
    "main bath": "primary bathroom",
    "main bathroom": "primary bathroom",
    "owners suite": "primary bedroom",
    "owner's suite": "primary bedroom",
    "powder bath": "powder room",
    "water closet": "bathroom",
    "florida room": "sunroom",
    "lanai": "sunroom",
    "sun porch": "sunroom",
}

# Words that carry a regional accent. Listed so the model recognises them
# rather than treating them as a room it does not have.
_REGIONAL_TERMS = {
    "northeast": (
        "parlor", "sitting room", "mudroom", "three-season porch",
        "finished basement", "powder room",
    ),
    "south": (
        "den", "sitting room", "keeping room", "bonus room", "mud room",
        "sunroom", "carport", "half bath", "breakfast nook",
    ),
    "midwest": (
        "front room", "rec room", "lower level", "washroom", "drop zone",
        "four-season room", "mud room",
    ),
    "west": (
        "great room", "bonus room", "den", "flex room", "casita", "lanai",
    ),
}

REGION_LABELS = {
    "northeast": "the Northeast",
    "south": "the South",
    "midwest": "the Midwest",
    "west": "the West",
}


def region_for_zip(zip_code: str | None) -> str | None:
    """The census region a five-digit US zip falls in, or None.

    Anything that is not a plain US zip -- a Canadian postal code, a partial,
    a typo -- returns None rather than a guess, and the caller simply gets no
    regional directive.
    """
    if not zip_code:
        return None
    digits = "".join(ch for ch in str(zip_code) if ch.isdigit())
    if len(digits) != 5:
        return None
    return _REGION_BY_FIRST_DIGIT.get(digits[0])


def directive_for(zip_code: str | None) -> str | None:
    """One directive line about room vocabulary, or None when the zip is
    unknown or unparseable. Never asserts what a room is."""
    region = region_for_zip(zip_code)
    if region is None:
        return None
    terms = ", ".join(_REGIONAL_TERMS[region])
    return (
        f"- ROOM VOCABULARY. This home is in {REGION_LABELS[region]}, where "
        f"these words for rooms are common: {terms}. If the homeowner uses "
        "one, take it as their name for that space and use it back to them "
        "from then on. Never correct their word for a room or explain that "
        "you would call it something else: the label in the scan is a guess "
        "and theirs is not."
    )
