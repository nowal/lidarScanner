"""Service-type normalization.

Found by the persona battery (Sep 3): the model captures the homeowner's
own words, so `projectType` was being stored as "kitchen repaint" or
"refresh". Those read fine in conversation and are useless downstream --
partner lookup keys off the service catalog, so a lead recorded that way
matched no partner at all and the ops email lost its partners-first
section.
"""

import pytest

from app.config import settings
from app.flow.partners import find_partners
from app.home_guide_tools import KNOWN_SERVICE_TYPES, normalize_service_type


@pytest.mark.parametrize("phrase,expected", [
    ("Painting", "Painting"),
    ("painting", "Painting"),
    ("  PAINTING  ", "Painting"),
    # The actual captured values from the battery transcripts:
    ("kitchen repaint", "Painting"),
    ("kitchen wall painting", "Painting"),
    ("wall painting", "Painting"),
    ("new hardwood floors", "Flooring"),
    ("deep clean before move-in", "Interior Cleaning"),
    ("deck railing", "Decking"),
    ("pressure wash the driveway", "Power Washing"),
])
def test_free_text_maps_onto_the_catalog(phrase, expected):
    assert normalize_service_type(phrase) == expected
    assert expected in KNOWN_SERVICE_TYPES


@pytest.mark.parametrize("phrase", ["refresh", "something nicer", "", None, "   "])
def test_unmappable_phrases_return_none_rather_than_a_guess(phrase):
    """A wrong trade on a lead package sends the homeowner the wrong
    provider -- better to keep the raw phrase and let a human read it."""
    assert normalize_service_type(phrase) is None


def test_partner_matching_survives_unnormalized_history(tmp_path, monkeypatch):
    """Records written before the fix must still match a partner."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    # A real row, not the seed: sample rows are never returned to any reader
    # (Sep 13), so the legacy-vs-canonical comparison needs data of its own.
    from app.flow import partners as _partners
    _partners._write_local([{
        "name": "Brightline Painting", "serviceTypes": ["Painting"], "zips": ["37203"],
        "relationship": "partner", "phone": "(615) 555-0100",
    }])
    canonical = find_partners("Painting", "37203")
    legacy = find_partners("kitchen repaint", "37203")
    assert canonical, "a real partner row should match the canonical service"
    assert [p["name"] for p in legacy] == [p["name"] for p in canonical]


def test_partner_matching_still_requires_a_real_service(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    assert find_partners("refresh", "37203") == []
    assert find_partners(None, "37203") == []
