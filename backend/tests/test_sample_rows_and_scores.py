"""Two things Nathan saw in a real lead email on Sep 13.

1. A row headed "SAMPLE ROW -- Partner Painting Co." listed first, tagged
   "TakeShape partner", with a caption saying to replace it. The seed exists
   so the local partner file is never empty; it is a placeholder, not a
   company. A caption is not a substitute for omission: nothing downstream
   of the row matcher may ever see one.

2. "score 0.00" on every provider. With discovery off (no Google Places key
   on the demo) no platform ever contributes a number, every row lands on
   the no-data baseline, and a column of identical zeros reads as a verdict
   on the companies rather than as an absence of data. The score is printed
   only when at least one platform actually scored.
"""

import pytest

from app.config import settings
from app.flow import partners
from app.flow.ops_email import _ranked_lines, provider_row_view
from app.flow.provider_ranking import RankingWeights, rank_candidates


@pytest.fixture(autouse=True)
def _empty_table(tmp_path, monkeypatch):
    """No local partner file and no durable rows: exactly the deployed demo
    on Sep 13, where `_load_rows` falls through to the labeled seed."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "supabase_url", "", raising=False)
    monkeypatch.setattr(settings, "supabase_service_role_key", "", raising=False)


# ------------------------------------------------------ 1. sample rows
def test_the_seed_is_the_only_source_in_this_fixture():
    """Sanity: the seed really is what the loader sees, and it is samples."""
    rows = partners._load_rows()
    assert rows, "seed should load when nothing else exists"
    assert all(r.get("sample") for r in rows)


@pytest.mark.parametrize("fn", [partners.find_partners, partners.find_prospects, partners.candidates])
def test_no_reader_ever_returns_a_sample_row(fn):
    out = fn("Painting", "37203")
    assert out == [], f"{fn.__name__} surfaced a sample row: {out}"


def test_the_ranked_list_never_contains_a_sample_row():
    ranked = partners.rank_for_lead("Painting", "37203", researched=[
        {"name": "Nash Painting", "phone": "(629) 263-7901", "serviceTypes": ["Painting"], "zips": ["37203"]},
    ])
    names = [e["name"] for e in ranked]
    assert "Nash Painting" in names
    assert not any("SAMPLE" in n.upper() for n in names)
    assert not any((e.get("row") or {}).get("sample") for e in ranked)


def test_a_sample_row_is_never_called_a_partner_when_a_real_row_exists(tmp_path):
    """The other half of the seed's job still holds: once a real row is
    written, it is what readers see, and the sample is gone from them too."""
    real = {"name": "Brightline Painting", "serviceTypes": ["Painting"], "zips": ["37203"],
            "relationship": "partner", "phone": "(615) 555-0100"}
    partners._write_local([real, {"name": "SAMPLE ROW", "serviceTypes": ["Painting"], "zips": ["37203"], "sample": True}])
    names = [r["name"] for r in partners.find_partners("Painting", "37203")]
    assert names == ["Brightline Painting"]


# ------------------------------------------------------ 2. score column
def _candidates_with_and_without_data():
    with_data = {
        "name": "Nash Painting", "relationship": "researched", "phone": "(629) 263-7901",
        "presences": [{"platform": "google", "profileUrl": "https://maps.example/nash",
                       "rating": 4.8, "reviewCount": 45, "source": "google_places_api",
                       "lastVerifiedAt": "2026-09-13T00:00:00+00:00"}],
    }
    without = {
        "name": "Sharpton Painting", "relationship": "researched", "phone": "(615) 582-0076",
        "presences": [],
    }
    return with_data, without


def test_no_data_rows_are_not_scored_and_the_line_omits_the_number():
    _, without = _candidates_with_and_without_data()
    ranked = rank_candidates([without], RankingWeights())
    view = provider_row_view(ranked[0])
    assert view["scored"] is False
    assert "no review or social data on file" in view["notes"]
    lines = _ranked_lines(ranked)
    row = next(l for l in lines if "Sharpton Painting" in l)
    # The header and attribution legitimately mention scores; the row must not.
    assert "score" not in row.lower()


def test_a_row_with_review_data_is_scored_and_the_line_shows_it():
    with_data, _ = _candidates_with_and_without_data()
    ranked = rank_candidates([with_data], RankingWeights())
    view = provider_row_view(ranked[0])
    assert view["scored"] is True
    assert view["score"] > 0
    text = "\n".join(_ranked_lines(ranked))
    assert "Nash Painting" in text
    assert f"score {view['score']:.2f}" in text


def test_mixed_list_prints_a_score_only_where_one_exists():
    with_data, without = _candidates_with_and_without_data()
    ranked = rank_candidates([with_data, without], RankingWeights())
    lines = _ranked_lines(ranked)
    nash = next(l for l in lines if "Nash Painting" in l)
    sharpton = next(l for l in lines if "Sharpton Painting" in l)
    assert "score" in nash
    assert "score" not in sharpton
