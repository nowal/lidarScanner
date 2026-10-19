"""Normalize only an extracted, verbatim ZIP span, never a city lookup."""
import re

DIGITS = dict(zip(('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine'), '0123456789'))
DIGITS['oh'] = '0'


def normalize_zip(raw: str) -> str | None:
    text = raw.strip().lower()
    # Individual spoken digits, optionally separated by commas or a ZIP+4 dash.
    parts = re.split(r'[\s,\-]+', text)
    if parts and all(p in DIGITS or re.fullmatch(r'[0-9]+', p) for p in parts):
        value = ''.join(DIGITS.get(p, p) for p in parts)
        if len(value) == 5:
            return value
        if len(value) == 9:
            return value[:5] + '-' + value[5:]
    return None
