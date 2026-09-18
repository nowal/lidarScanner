"""Provider discovery + ranking: the durable provider table, platform
presences, and the score that orders the ops email's suggestions.

Fixture: tests/fixtures/provider_candidates.json -- fictional rows shaped
like the flow_partners table. Deliberately NOT demo_assets/partners_seed.json,
which is demo fixture data and says nothing about ranking behaviour.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import app.flow.local_research as lr
from app.config import settings
from app.flow import partners, supabase_store
from app.flow.ops_email import build_ops_email, build_ops_email_html
from app.flow.provider_ranking import (
    RankingWeights,
    explain,
    parse_platform_weights,
    percentile_rank,
    rank_candidates,
)
from app.flow_quotes import QuoteRequestRecord

FIXTURE = Path(__file__).parent / "fixtures" / "provider_candidates.json"


def _fixture_rows(service: str | None = None) -> list[dict]:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]
    if service:
        rows = [r for r in rows if service in r["serviceTypes"]]
    return [dict(r, relationship=partners.relationship(r)) for r in rows]


def _by_name(ranked: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in ranked}


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "preferred_partner_ordering_enabled", False)
    partners._legacy_warned = False
    yield tmp_path


def _install_table(tmp_path: Path) -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]
    (tmp_path / "partners.json").write_text(json.dumps(rows), encoding="utf-8")


def _record(**overrides) -> QuoteRequestRecord:
    base = dict(
        id="qr_rank01", createdAt="2026-09-09T12:00:00+00:00", threadId="t-rank",
        status="submitted", serviceType="Painting", zip="37203", firstName="Dana",
        contactEmail="dana@example.com", synopsis="Dana wants the kitchen painted.",
        measurements={"floorAreaSquareFeet": 420},
    )
    base.update(overrides)
    return QuoteRequestRecord(**base)


# ------------------------------------------------------------ the scenarios
def test_strong_on_one_platform_absent_elsewhere():
    ranked = _by_name(rank_candidates(_fixture_rows("Painting")))
    cumberland = ranked["Cumberland Coatings"]
    comps = cumberland["components"]
    assert list(comps["platforms"]) == ["google"], "absent platforms are absent, not zero"
    assert "yelp" not in comps["platforms"] and "instagram" not in comps["platforms"]
    assert comps["multiPlatformBonus"] == 0.0
    assert comps["dataCoverage"] == "full"
    assert comps["presenceScore"] == comps["platforms"]["google"]["score"]
    # The strongest Google presence in the set sits at the top of its percentile.
    assert comps["platforms"]["google"]["volumePercentile"] == pytest.approx(0.9)
    assert cumberland["rank"] == 1


def test_moderately_present_on_all_platforms_earns_the_bonus():
    ranked = _by_name(rank_candidates(_fixture_rows("Painting")))
    belmont = ranked["Belmont Brush Co."]
    comps = belmont["components"]
    assert sorted(comps["scoredPlatforms"]) == ["facebook", "google", "instagram", "yelp"]
    w = RankingWeights()
    assert comps["multiPlatformBonus"] == pytest.approx(min(w.multi_platform_bonus_cap, w.multi_platform_bonus * 3))
    # Follower-only platforms score on volume alone; no rating is invented.
    assert comps["platforms"]["instagram"]["ratingScore"] is None
    assert comps["platforms"]["instagram"]["rating"] is None
    assert comps["platforms"]["instagram"]["volume"] == 800
    # Weighted mean over present platforms, not a raw sum across them.
    total_w = sum(w.platform_weights[p] for p in comps["scoredPlatforms"])
    expected = sum(w.platform_weights[p] * comps["platforms"][p]["score"] for p in comps["scoredPlatforms"]) / total_w
    assert comps["presenceScore"] == pytest.approx(expected, abs=1e-3)
    assert belmont["rank"] == 2  # above Harpeth's single strong platform


def test_high_rating_on_very_few_reviews_is_shrunk():
    ranked = _by_name(rank_candidates(_fixture_rows("Painting")))
    few = ranked["Twelve South Painting"]["components"]["platforms"]["google"]
    many = ranked["Harpeth Finishes"]["components"]["platforms"]["google"]
    assert few["rating"] == 5.0 and few["reviewCount"] == 3
    assert many["rating"] == 4.6 and many["reviewCount"] == 150
    assert few["ratingScore"] < many["ratingScore"], "five stars on three reviews trusts the prior"
    assert ranked["Harpeth Finishes"]["score"] > ranked["Twelve South Painting"]["score"]


def test_a_candidate_set_with_no_social_data_at_all():
    ranked = rank_candidates(_fixture_rows("Interior Cleaning"))
    assert [e["name"] for e in ranked] == [
        "Sparkle & Co. Cleaning",   # the only signal in the set: it has quoted
        "Fresh Nest Cleaning",      # then relationship, then name -- deterministic
        "Germantown Home Care",
    ]
    for entry in ranked:
        comps = entry["components"]
        assert comps["presenceScore"] is None, "no data is None, never a zero score"
        assert comps["dataCoverage"] == "none"
        assert comps["platforms"] == {}
        assert entry["score"] == pytest.approx(comps["baseline"] + comps["quotedBoost"])
    assert ranked[0]["components"]["quotedBoost"] > 0
    assert "no review or social data" in explain(ranked[1])


def test_a_category_with_fewer_than_three_candidates():
    two = rank_candidates(_fixture_rows("Flooring"))
    assert len(two) == 2 and all(e["components"]["smallSet"] for e in two)
    assert [e["name"] for e in two] == ["Hardwood Haven", "Plank & Grain"]
    pct = {e["name"]: e["components"]["platforms"]["google"]["volumePercentile"] for e in two}
    assert pct == {"Hardwood Haven": pytest.approx(0.75), "Plank & Grain": pytest.approx(0.25)}
    assert "small local set" in explain(two[0])

    one = rank_candidates(_fixture_rows("Decking"))
    assert len(one) == 1 and one[0]["rank"] == 1
    assert one[0]["components"]["platforms"]["google"]["volumePercentile"] == pytest.approx(0.5)
    # Percentile alone would call a lone 61-review business the top of its
    # market; the log term keeps an absolute sense of volume.
    assert one[0]["components"]["platforms"]["google"]["volumeLog"] < 1.0

    assert rank_candidates([]) == []


def test_a_previously_quoted_provider_with_weak_social_presence():
    rows = _fixture_rows("Painting")
    with_boost = _by_name(rank_candidates(rows))
    music = with_boost["Music City Paint Pros"]
    comps = music["components"]
    assert music["relationship"] == "quoted" and comps["quotedCount"] == 3
    assert comps["quotedBoost"] > 0, "the note_quoted signal is an explicit named term"
    assert music["score"] == pytest.approx(
        comps["presenceScore"] + comps["quotedBoost"] + comps["multiPlatformBonus"], abs=1e-3
    )
    assert "quoted boost" in explain(music)
    # Its Google presence alone (3.8 on 3 reviews) loses to five stars on
    # three reviews; the quote history is what lifts it above.
    assert music["rank"] < with_boost["Twelve South Painting"]["rank"]

    without = _by_name(rank_candidates(rows, RankingWeights(quoted_boost=0.0)))
    assert without["Music City Paint Pros"]["components"]["quotedBoost"] == 0.0
    assert without["Music City Paint Pros"]["rank"] > without["Twelve South Painting"]["rank"]
    assert without["Music City Paint Pros"]["score"] == pytest.approx(music["score"] - comps["quotedBoost"], abs=1e-3)


def test_a_profile_link_without_numbers_is_recorded_but_not_scored():
    ranked = _by_name(rank_candidates(_fixture_rows("Painting")))
    nolensville = ranked["Nolensville Wall Works"]["components"]
    assert nolensville["dataCoverage"] == "links_only"
    assert nolensville["platforms"]["yelp"]["profileUrl"].startswith("https://www.yelp.com/biz/")
    assert nolensville["platforms"]["yelp"]["score"] is None
    assert nolensville["presenceScore"] is None
    assert "yelp: profile link only" in explain(ranked["Nolensville Wall Works"])


def test_every_stored_number_carries_its_source_and_timestamp():
    ranked = rank_candidates(_fixture_rows("Painting"))
    for entry in ranked:
        for platform, comp in entry["components"]["platforms"].items():
            assert comp["source"], f"{entry['name']}/{platform} has no source"
            assert comp["lastVerifiedAt"], f"{entry['name']}/{platform} has no timestamp"


# ---------------------------------------------------------- configuration
def test_weights_come_from_configuration(monkeypatch):
    monkeypatch.setattr(settings, "rank_platform_weights", "google=2.0,yelp=0.1,tiktok=0.3")
    monkeypatch.setattr(settings, "rank_quoted_boost", 0.9)
    monkeypatch.setattr(settings, "rank_multi_platform_bonus", 0.0)
    w = RankingWeights.from_settings()
    assert w.platform_weights == {"google": 2.0, "yelp": 0.1, "tiktok": 0.3}
    assert w.quoted_boost == 0.9 and w.multi_platform_bonus == 0.0
    assert parse_platform_weights("google=1,bad,yelp=x") == {"google": 1.0}
    assert "quotedBoost" in w.to_json()


def test_trade_category_is_configuration_not_code(monkeypatch):
    monkeypatch.setattr(settings, "provider_discovery_queries", '{"Roofing": "roofing contractor", "painting": "house painter"}')
    assert lr.discovery_query("Roofing") == "roofing contractor"
    assert lr.discovery_query("Painting") == "house painter"           # case-insensitive
    assert lr.discovery_query("Gutter Cleaning") == "gutter cleaning contractor"  # fallback
    monkeypatch.setattr(settings, "provider_discovery_queries", "not json")
    assert lr.discovery_query("Painting") == "painting contractor"


def test_percentile_is_mid_rank():
    assert percentile_rank(5.0, [5.0]) == 0.5
    assert percentile_rank(1.0, [1.0, 2.0]) == 0.25
    assert percentile_rank(2.0, [1.0, 2.0]) == 0.75
    assert percentile_rank(3.0, [3.0, 3.0, 3.0]) == 0.5


# ------------------------------------------------------ deprecated ordering
def test_the_deprecated_partner_first_ordering_still_works_behind_its_flag(monkeypatch, tmp_path, caplog):
    _install_table(tmp_path)
    monkeypatch.setattr(settings, "preferred_partner_ordering_enabled", True)
    ranked = partners.rank_for_lead("Painting", "37203")
    assert ranked[0]["name"] == "Riverbend Interiors" and ranked[0]["relationship"] == "partner"
    assert ranked[1]["name"] == "Music City Paint Pros" and ranked[1]["relationship"] == "quoted"
    assert any("deprecated" in r.message for r in caplog.records)
    # Default: the same rows ordered by score, the partner with no data last
    # among the scored ones.
    monkeypatch.setattr(settings, "preferred_partner_ordering_enabled", False)
    ranked = partners.rank_for_lead("Painting", "37203")
    assert ranked[0]["name"] == "Cumberland Coatings"
    scores = [e["score"] for e in ranked]
    assert scores == sorted(scores, reverse=True)


# ----------------------------------------------------------- the ops email
def test_the_email_renders_the_ranked_list_and_keeps_the_word_partner_reserved(tmp_path):
    _install_table(tmp_path)
    ranked = partners.rank_for_lead("Painting", "37203", researched=[{"name": "Found Online LLC", "phone": "(615) 555-0199"}])
    assert [e["relationship"] for e in ranked if e["name"] == "Found Online LLC"] == ["researched"]

    subject, body = build_ops_email(_record(), partners.find_partners("Painting", "37203"), None,
                                    partners.find_prospects("Painting", "37203"), ranked)
    assert "NO PROVIDER COVERAGE" not in subject
    section = body.split("SUGGESTED PROVIDERS")[1].split("ENTER THE CHECKED QUOTE")[0]
    assert "Ranked by review and social presence" in section
    assert section.count("TakeShape partner") == 1 and "Riverbend Interiors | TakeShape partner" in section
    assert "[PAST QUOTER x3] Music City Paint Pros | quoted 3x through TakeShape" in section
    assert "Found Online LLC | found online, unvetted" in section
    assert "1. Cumberland Coatings" in section and "score" in section
    assert "Google: 4.8 stars, 412 reviews (verified 2026-09-08) https://maps.google.com/?cid=1001" in section
    assert "quoted boost" in section
    assert "via Google Places" in section and "never scraped" in section
    assert "contact first" not in section, "the partner-first wording belongs to the deprecated path"

    html = build_ops_email_html(_record(), [], None, [], ranked)
    assert "Cumberland Coatings" in html and "TakeShape partner" in html and "score 0." in html


def test_the_legacy_flag_restores_the_grouped_section(monkeypatch, tmp_path):
    _install_table(tmp_path)
    monkeypatch.setattr(settings, "preferred_partner_ordering_enabled", True)
    ranked = partners.rank_for_lead("Painting", "37203")
    _, body = build_ops_email(_record(), partners.find_partners("Painting", "37203"), None,
                              partners.find_prospects("Painting", "37203"), ranked)
    assert "TakeShape partners for this area (contact first):" in body
    assert "Ranked by review" not in body


# ----------------------------------------------------- discovery merge rules
def test_discovery_never_overwrites_what_note_quoted_wrote(tmp_path):
    row = partners.record_quoted_provider("Music City Paint Pros", "Painting", "37203",
                                          provider_id="prov-9", contact={"phone": "(615) 555-0145"})
    assert row["quotedCount"] == 1 and row["relationship"] == "quoted"
    stamp = row["lastQuotedAt"]

    changed = partners.attach_discovery(
        [{"name": "music city paint pros", "phone": "(615) 555-9999", "website": "https://mcpp.example",
          "presences": [{"platform": "google", "profileUrl": "https://maps.google.com/?cid=5", "rating": 3.8,
                         "reviewCount": 3, "source": "google_places_api",
                         "lastVerifiedAt": "2026-09-09T10:00:00+00:00", "placeId": "ChIJx"}]},
         {"name": "New Discovery Painting", "presences": [
             {"platform": "google", "rating": None, "reviewCount": None, "source": "google_places_api"}]}],
        "Painting", "37203", source="google_places",
    )
    assert changed == 2
    rows = {r["name"].lower(): r for r in json.loads((tmp_path / "partners.json").read_text(encoding="utf-8"))}
    music = rows["music city paint pros"]
    assert music["relationship"] == "quoted" and music["quotedCount"] == 1 and music["lastQuotedAt"] == stamp
    assert music["providerId"] == "prov-9" and music["phone"] == "(615) 555-0145", "existing contact kept"
    assert music["website"] == "https://mcpp.example", "an empty field is filled"
    assert music["presences"][0]["reviewCount"] == 3 and music["presences"][0]["placeId"] == "ChIJx"
    new = rows["new discovery painting"]
    assert new["relationship"] == "prospect" and new["source"] == "google_places"
    assert new["presences"][0]["rating"] is None and new["presences"][0]["reviewCount"] is None
    assert "quotedCount" not in new

    # A second, identical pass is a no-op -- nothing rewritten.
    assert partners.attach_discovery(
        [{"name": "New Discovery Painting", "presences": [
            {"platform": "google", "rating": None, "reviewCount": None, "source": "google_places_api",
             "lastVerifiedAt": new["presences"][0]["lastVerifiedAt"]}]}],
        "Painting", "37203", source="google_places") == 0


def test_merge_presence_refreshes_numbers_but_keeps_a_known_link():
    row = {"name": "X", "presences": [
        {"platform": "yelp", "profileUrl": "https://www.yelp.com/biz/x", "rating": None, "reviewCount": None,
         "followerCount": None, "source": "web_search_profile_link", "lastVerifiedAt": "2026-08-01T00:00:00+00:00"}]}
    assert partners.merge_presence(row, {"platform": "Yelp", "rating": 4.1, "reviewCount": 0, "source": "ops_entry"})
    yelp = row["presences"][0]
    assert yelp["profileUrl"] == "https://www.yelp.com/biz/x", "a link found earlier is not erased"
    assert yelp["rating"] == 4.1 and yelp["reviewCount"] == 0, "a real zero from a source is kept"
    assert yelp["source"] == "ops_entry" and yelp["lastVerifiedAt"] > "2026-08-01"
    assert partners.normalize_presence({"platform": ""}) is None
    clean = partners.normalize_presence({"platform": "google", "rating": "", "reviewCount": "n/a"})
    assert clean["rating"] is None and clean["reviewCount"] is None


# --------------------------------------------------------- Google Places
@pytest.mark.asyncio
async def test_places_search_records_source_timestamp_and_nullable_numbers(monkeypatch):
    monkeypatch.setattr(settings, "google_places_api_key", "places-key")
    monkeypatch.setattr(settings, "provider_discovery_queries", '{"Painting": "painting contractor"}')
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"places": [
            {"id": "ChIJa", "displayName": {"text": "Cumberland Coatings"}, "rating": 4.8, "userRatingCount": 412,
             "googleMapsUri": "https://maps.google.com/?cid=1", "websiteUri": "https://cc.example",
             "nationalPhoneNumber": "(615) 555-0140", "formattedAddress": "1 Main St, Nashville, TN"},
            {"id": "ChIJb", "displayName": {"text": "Quiet Brush LLC"}},   # no rating fields at all
            {"id": "ChIJc", "displayName": {"text": "local painters"}},   # generic name, dropped
        ]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    found = await lr.search_places("Painting", "37203")
    assert seen["url"] == lr.PLACES_SEARCH_URL
    assert seen["headers"]["x-goog-api-key"] == "places-key"
    assert seen["headers"]["x-goog-fieldmask"] == lr.PLACES_FIELD_MASK
    assert seen["body"]["textQuery"] == "painting contractor near 37203"
    assert [f["name"] for f in found] == ["Cumberland Coatings", "Quiet Brush LLC"]
    google = found[0]["presences"][0]
    assert google["platform"] == "google" and google["rating"] == 4.8 and google["reviewCount"] == 412
    assert google["source"] == lr.PLACES_SOURCE and google["lastVerifiedAt"] and google["placeId"] == "ChIJa"
    assert found[0]["phone"] == "(615) 555-0140" and found[0]["website"] == "https://cc.example"
    quiet = found[1]["presences"][0]
    assert quiet["rating"] is None and quiet["reviewCount"] is None, "absent is null, never zero"


@pytest.mark.asyncio
async def test_places_search_without_a_key_or_on_error_is_no_data(monkeypatch):
    monkeypatch.setattr(settings, "google_places_api_key", "")
    assert await lr.search_places("Painting", "37203") is None
    monkeypatch.setattr(settings, "google_places_api_key", "k")
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error": "denied"})), **kw))
    assert await lr.search_places("Painting", "37203") is None


# ----------------------------------------------------- link-only platforms
def test_profile_links_are_accepted_only_on_the_platforms_own_host():
    ok = lr.accept_profile_url
    assert ok("yelp", "https://www.yelp.com/biz/belmont-brush-co") == "https://www.yelp.com/biz/belmont-brush-co"
    assert ok("facebook", "facebook.com/belmontbrushco") == "https://facebook.com/belmontbrushco"
    assert ok("yelp", "https://www.yelp.com/") is None, "the site root is not a profile"
    assert ok("yelp", "https://yelp.com.evil.example/biz/x") is None
    assert ok("instagram", "https://www.facebook.com/x") is None, "wrong platform"
    assert ok("nextdoor", "") is None
    assert ok("yelp", "javascript:alert(1)") is None


@pytest.mark.asyncio
async def test_profile_link_discovery_keeps_counts_null(monkeypatch):
    async def fake_search(prompt, schema, *, max_uses, timeout=50.0, max_tokens=2500):
        assert "Do not report ratings or follower counts" in prompt
        return {"businesses": [
            {"name": "Cumberland Coatings", "yelpUrl": "https://www.yelp.com/biz/cumberland-coatings",
             "facebookUrl": "https://www.facebook.com/", "instagramUrl": "https://www.instagram.com/cumberlandcoatings/",
             "nextdoorUrl": ""},
            {"name": "Somebody Else", "yelpUrl": "https://www.yelp.com/biz/x", "facebookUrl": "", "instagramUrl": "", "nextdoorUrl": ""},
        ]}
    monkeypatch.setattr(lr, "_search", fake_search)
    links = await lr.lookup_profile_links(["Cumberland Coatings", "Harpeth Finishes"], "37203")
    assert links == {"Cumberland Coatings": {
        "yelp": "https://www.yelp.com/biz/cumberland-coatings",
        "instagram": "https://www.instagram.com/cumberlandcoatings/",
    }}, "root links dropped, unrequested names ignored"


@pytest.mark.asyncio
async def test_the_discovery_pass_is_gated_cached_and_merged(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "provider_discovery_enabled", False)
    assert await lr.discover_providers("Painting", "37203") is None

    monkeypatch.setattr(settings, "provider_discovery_enabled", True)
    calls = {"places": 0, "links": 0}

    async def fake_places(service, zip_code):
        calls["places"] += 1
        return [{"name": "Cumberland Coatings", "phone": "(615) 555-0140", "website": "", "address": "",
                 "presences": [{"platform": "google", "profileUrl": "https://maps.google.com/?cid=1", "rating": 4.8,
                                "reviewCount": 412, "followerCount": None, "source": lr.PLACES_SOURCE,
                                "lastVerifiedAt": "2026-09-09T10:00:00+00:00", "placeId": "ChIJa"}]}]

    async def fake_links(names, zip_code):
        calls["links"] += 1
        return {"Cumberland Coatings": {"yelp": "https://www.yelp.com/biz/cumberland-coatings"}}

    monkeypatch.setattr(lr, "search_places", fake_places)
    monkeypatch.setattr(lr, "lookup_profile_links", fake_links)

    found = await lr.discover_providers("Painting", "37203")
    assert [p["platform"] for p in found[0]["presences"]] == ["google", "yelp"]
    yelp = found[0]["presences"][1]
    assert yelp["rating"] is None and yelp["reviewCount"] is None and yelp["followerCount"] is None
    assert yelp["source"] == lr.PROFILE_LINK_SOURCE

    rows = json.loads((tmp_path / "partners.json").read_text(encoding="utf-8"))
    assert rows[-1]["name"] == "Cumberland Coatings" and rows[-1]["relationship"] == "prospect"
    assert rows[-1]["source"] == "google_places" and len(rows[-1]["presences"]) == 2
    assert (tmp_path / "local_research" / "discovery_painting_37203.json").exists()

    # Second call within the TTL: served from cache, no new API calls.
    await lr.discover_providers("Painting", "37203")
    assert calls == {"places": 1, "links": 1}
    ranked = partners.rank_for_lead("Painting", "37203")
    assert ranked[0]["name"] == "Cumberland Coatings"
    assert ranked[0]["components"]["dataCoverage"] == "partial"  # google scored, yelp link-only


# -------------------------------------------------- restart mid-flow (durable)
@pytest.fixture
def durable(monkeypatch):
    """A stand-in for the flow_partners table."""
    store: dict[str, dict] = {}

    async def list_rows():
        return [dict(v) for v in store.values()]

    async def upsert(entries):
        for e in entries:
            store[e["key"]] = e["record"]
        return True

    monkeypatch.setattr(supabase_store, "enabled", lambda: True)
    monkeypatch.setattr(supabase_store, "list_partner_rows", list_rows)
    monkeypatch.setattr(supabase_store, "upsert_partner_rows", upsert)
    return store


@pytest.mark.asyncio
async def test_the_provider_table_survives_a_restart_mid_flow(durable, tmp_path):
    # Step 10: a real quote lands and the company is promoted.
    row = partners.record_quoted_provider("Nash Painting", "Painting", "37203", contact={"phone": "(615) 555-0102"})
    await partners.flush_durable_writes()
    assert "nash painting" in durable and durable["nash painting"]["quotedCount"] == 1

    # Discovery attaches numbers to the same row; the durable copy follows.
    partners.attach_discovery([{"name": "Nash Painting", "presences": [
        {"platform": "google", "rating": 4.4, "reviewCount": 57, "source": "google_places_api"}]}],
        "Painting", "37203", source="google_places")
    await partners.flush_durable_writes()
    assert durable["nash painting"]["presences"][0]["reviewCount"] == 57
    assert durable["nash painting"]["quotedCount"] == 1

    # A redeploy: empty disk, cold process. The local file is gone and the
    # sync readers would otherwise fall back to the sample seed.
    (tmp_path / "partners.json").unlink()
    assert all(r.get("sample") for r in partners._load_rows()), "disk really is wiped"
    assert partners.find_prospects("Painting", "37203") == []

    assert await partners.rehydrate() == "rehydrated"
    prospects = partners.find_prospects("Painting", "37203")
    assert [p["name"] for p in prospects] == ["Nash Painting"]
    assert prospects[0]["quotedCount"] == 1 and prospects[0]["phone"] == "(615) 555-0102"
    assert prospects[0]["presences"][0]["reviewCount"] == 57
    ranked = partners.rank_for_lead("Painting", "37203")
    assert ranked[0]["name"] == "Nash Painting" and ranked[0]["components"]["quotedBoost"] > 0
    assert not any(r.get("sample") for r in partners._load_rows()), "fixture rows do not come back"

    # The next quote after the restart builds on the durable count.
    row = partners.record_quoted_provider("nash painting", "Painting", "37212")
    await partners.flush_durable_writes()
    assert row["quotedCount"] == 2 and durable["nash painting"]["quotedCount"] == 2
    assert durable["nash painting"]["zips"] == ["37203", "37212"]


@pytest.mark.asyncio
async def test_an_empty_table_is_bootstrapped_from_the_local_file_not_wiped(durable, tmp_path):
    (tmp_path / "partners.json").write_text(json.dumps([
        {"name": "Partner Painting Co.", "serviceTypes": ["Painting"], "zips": ["37203"]}
    ]), encoding="utf-8")
    assert await partners.rehydrate() == "bootstrapped"
    assert durable["partner painting co."]["name"] == "Partner Painting Co."
    assert partners.find_partners("Painting", "37203")[0]["name"] == "Partner Painting Co."


@pytest.mark.asyncio
async def test_rehydrate_without_supabase_leaves_the_local_copy_alone(tmp_path):
    (tmp_path / "partners.json").write_text(json.dumps([{"name": "Local Only", "serviceTypes": ["Painting"], "zips": ["37203"]}]))
    assert await partners.rehydrate() == "unavailable"
    assert partners.find_partners("Painting", "37203")[0]["name"] == "Local Only"


@pytest.mark.asyncio
async def test_the_real_durable_write_and_read_run_over_http(monkeypatch):
    """The other durability tests fake the store functions; this drives the
    real PostgREST calls with only the transport faked."""
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(settings, "supabase_service_role_key", "service-key")
    table: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer service-key"
        if request.method == "POST" and request.url.path.endswith("/flow_partners"):
            assert request.headers["prefer"] == "resolution=merge-duplicates"
            for row in json.loads(request.content):
                table[row["key"]] = row
            return httpx.Response(201, json=[])
        if request.method == "GET" and request.url.path.endswith("/flow_partners"):
            assert request.url.params["select"] == "record"
            return httpx.Response(200, json=[{"record": r["record"]} for r in table.values()])
        return httpx.Response(404)

    client = httpx.AsyncClient(
        base_url="https://example.supabase.co/rest/v1",
        headers={"Authorization": "Bearer service-key", "apikey": "service-key"},
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(supabase_store, "_client", client)
    monkeypatch.setattr(supabase_store, "_client_key", (settings.supabase_url, settings.supabase_service_role_key))

    assert await supabase_store.upsert_partner_rows([
        {"key": "nash painting", "name": "Nash Painting", "relationship": "quoted",
         "record": {"name": "Nash Painting", "quotedCount": 1}}]) is True
    assert table["nash painting"]["relationship"] == "quoted"
    assert await supabase_store.list_partner_rows() == [{"name": "Nash Painting", "quotedCount": 1}]
    assert await supabase_store.upsert_partner_rows([]) is False


# ------------------------------------------------ the lead email end to end
@pytest.mark.asyncio
async def test_send_ops_email_rehydrates_discovers_and_ranks(monkeypatch, tmp_path, durable):
    from app.flow import ops_email

    monkeypatch.setattr(settings, "ops_email", "ops@example.com")   # outbox capture, no transport
    monkeypatch.setattr(settings, "provider_discovery_enabled", True)
    durable["harpeth finishes"] = {
        "name": "Harpeth Finishes", "serviceTypes": ["Painting"], "zips": ["37203"], "zipPrefixes": ["372"],
        "relationship": "quoted", "source": "returned_quote", "quotedCount": 2,
    }
    discovered = {"called": 0}

    async def fake_discover(service, zip_code):
        discovered["called"] += 1
        partners.attach_discovery([{"name": "Harpeth Finishes", "presences": [
            {"platform": "google", "rating": 4.6, "reviewCount": 150, "source": "google_places_api"}]},
            {"name": "Cumberland Coatings", "presences": [
                {"platform": "google", "rating": 4.8, "reviewCount": 412, "source": "google_places_api"}]}],
            service, zip_code, source="google_places")
        return [{"name": "Harpeth Finishes"}, {"name": "Cumberland Coatings"}]

    monkeypatch.setattr(lr, "discover_providers", fake_discover)
    assert await ops_email.send_ops_email(_record()) == "outbox"
    assert discovered["called"] == 1
    outbox = json.loads((tmp_path / "ops_outbox" / "qr_rank01.json").read_text(encoding="utf-8"))
    body = outbox["body"]
    assert "Ranked by review and social presence" in body
    assert body.index("1. Cumberland Coatings") < body.index("2. [PAST QUOTER x2] Harpeth Finishes")
    assert "Harpeth Finishes | quoted 2x through TakeShape" in body
    assert "[NO PROVIDER COVERAGE]" not in outbox["subject"]
