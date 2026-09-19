"""A suggested provider operations cannot call or click is not a suggestion.

Nathan's Sep 13 lead email listed "Alvin Krantz Painting, Inc" and "PaintPro"
with nothing under the name: no phone, no website. The research schema marks
both fields required, but an empty string satisfies a required string, and
nothing downstream filtered on it. Two layers now do:

* ``lookup_provider_leads`` drops such entries at the source, so the cache
  never holds them and the count is logged;
* ``rank_for_lead`` skips them if one arrives by any other route.

A Places key is what fills these in properly (``attach_discovery`` copies
phone and website onto a name-matched row); until then, an unreachable lead
is dropped rather than shown.
"""

import asyncio

import pytest

from app.config import settings
from app.flow import local_research, partners


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "provider_finder_enabled", True)
    monkeypatch.setattr(settings, "supabase_url", "", raising=False)
    monkeypatch.setattr(settings, "supabase_service_role_key", "", raising=False)


def _parsed(*providers):
    return {"regionLabel": "Nashville, TN", "providers": list(providers)}


def _entry(name, phone="", website="", found=True):
    return {"name": name, "phone": phone, "website": website, "ratingLabel": "",
            "note": "painting", "foundOnline": found}


# ------------------------------------------------------------ at the source
def test_research_drops_a_lead_with_no_phone_and_no_website(monkeypatch):
    async def fake_search(prompt, schema, **kw):
        return _parsed(
            _entry("Nash Painting", phone="(629) 263-7901", website="https://www.nashpainting.com"),
            _entry("PaintPro"),                                   # nothing at all
            _entry("Pointer Painting", website="https://pointerpainting.com/"),
            _entry("Alvin Krantz Painting, Inc"),                 # nothing at all
            _entry("Sharpton Painting", phone="(615) 582-0076"),
        )
    monkeypatch.setattr(local_research, "_search", fake_search)
    leads = asyncio.run(local_research.lookup_provider_leads("Painting", "37203"))
    names = [l["name"] for l in leads]
    assert names == ["Nash Painting", "Pointer Painting", "Sharpton Painting"]
    assert all(l["phone"] or l["website"] for l in leads)


def test_website_only_and_phone_only_both_count_as_reachable(monkeypatch):
    async def fake_search(prompt, schema, **kw):
        return _parsed(_entry("A", website="https://a.example"), _entry("B", phone="(615) 000-0000"))
    monkeypatch.setattr(local_research, "_search", fake_search)
    leads = asyncio.run(local_research.lookup_provider_leads("Painting", "37203"))
    assert [l["name"] for l in leads] == ["A", "B"]


def test_all_unreachable_means_no_leads_not_a_list_of_names(monkeypatch):
    """Then the email falls to its NO PROVIDER COVERAGE state, which is honest,
    instead of five names nobody can act on."""
    async def fake_search(prompt, schema, **kw):
        return _parsed(_entry("Ghost One"), _entry("Ghost Two"))
    monkeypatch.setattr(local_research, "_search", fake_search)
    assert asyncio.run(local_research.lookup_provider_leads("Painting", "37203")) is None


def test_the_cache_never_holds_an_unreachable_lead(monkeypatch):
    calls = {"n": 0}
    async def fake_search(prompt, schema, **kw):
        calls["n"] += 1
        return _parsed(_entry("Reachable", phone="(615) 111-1111"), _entry("Unreachable"))
    monkeypatch.setattr(local_research, "_search", fake_search)
    first = asyncio.run(local_research.lookup_provider_leads("Painting", "37203"))
    second = asyncio.run(local_research.lookup_provider_leads("Painting", "37203"))
    assert calls["n"] == 1, "second call should be served from cache"
    assert [l["name"] for l in first] == [l["name"] for l in second] == ["Reachable"]


# ---------------------------------------------------------- in the ranker
def test_rank_for_lead_skips_a_researched_lead_with_no_contact():
    ranked = partners.rank_for_lead("Painting", "37203", researched=[
        {"name": "Nash Painting", "phone": "(629) 263-7901"},
        {"name": "PaintPro"},
        {"name": "Pointer Painting", "website": "https://pointerpainting.com/"},
    ])
    names = [e["name"] for e in ranked]
    assert "PaintPro" not in names
    assert names == ["Nash Painting", "Pointer Painting"] or set(names) == {"Nash Painting", "Pointer Painting"}


def test_rank_for_lead_treats_whitespace_contact_as_missing():
    ranked = partners.rank_for_lead("Painting", "37203", researched=[
        {"name": "Blank Co", "phone": "   ", "website": ""},
        {"name": "Real Co", "phone": "(615) 222-2222"},
    ])
    assert [e["name"] for e in ranked] == ["Real Co"]
