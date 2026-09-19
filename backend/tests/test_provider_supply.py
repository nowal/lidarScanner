"""Provider supply: partners stay partners, prospects stay prospects, a company
that quotes gets remembered, and an empty list is said out loud.

TakeShape has two painting partners. Nearly every lead is served by research
and Quintin's phone calls, so the list has to grow out of ordinary work, and
the ops email must never call a stranger a partner.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.flow import supabase_store
from app.flow.ops_email import build_ops_email, build_ops_email_html
from app.flow.partners import (
    coverage_gap,
    find_partners,
    find_prospects,
    import_prospects,
    record_quoted_provider,
    relationship,
)
from app.flow_quotes import QuoteRequestRecord, quote_store
from app.main import app


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    supabase_store._homeowner_cache.clear()
    yield tmp_path
    supabase_store._homeowner_cache.clear()


def _record(**overrides) -> QuoteRequestRecord:
    base = dict(
        id="qr_supply01", createdAt="2026-09-07T12:00:00+00:00", threadId="t-supply",
        status="submitted", serviceType="Painting", zip="37203", firstName="Dana",
        contactEmail="dana@example.com", synopsis="Dana wants the kitchen painted.",
        measurements={"floorAreaSquareFeet": 420},
    )
    base.update(overrides)
    return QuoteRequestRecord(**base)


def _rows(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "partners.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------- relationship
def test_hand_entered_rows_are_partners_and_imported_rows_are_not():
    assert relationship({"name": "A"}) == "partner"
    assert relationship({"name": "B", "source": "sheet:providers"}) == "prospect"
    assert relationship({"name": "C", "relationship": "quoted"}) == "quoted"
    assert relationship({"name": "D", "relationship": "PARTNER", "source": "x"}) == "partner"


def test_an_imported_list_never_becomes_partners(tmp_path):
    added = import_prospects(
        [{"name": "Nash Painting", "serviceTypes": ["Painting"], "zips": ["37203"],
          "relationship": "partner"},          # the sheet says partner; we do not believe it
         {"name": "Nash Painting"},            # duplicate, skipped
         {"name": "", "serviceTypes": ["Painting"]}],
        source="sheet:providers",
    )
    assert added == 1
    partners = [r["name"] for r in find_partners("Painting", "37203")]
    assert "Nash Painting" not in partners
    prospects = find_prospects("Painting", "37203")
    assert [p["name"] for p in prospects] == ["Nash Painting"]
    assert prospects[0]["relationship"] == "prospect"
    assert prospects[0]["source"] == "sheet:providers"


def test_import_needs_a_source_label():
    with pytest.raises(ValueError):
        import_prospects([{"name": "X"}], source="")


# ------------------------------------------------------------- promotion
def test_a_returned_quote_records_the_company_as_quoted_not_partner(tmp_path):
    row = record_quoted_provider("Brightline Painting", "painting", "37203-1234",
                                 provider_id="prov-1", contact={"phone": "(615) 555-0101"})
    assert row["relationship"] == "quoted"
    assert row["serviceTypes"] == ["Painting"]           # normalised onto the catalog
    assert row["zips"] == ["37203"] and row["zipPrefixes"] == ["372"]
    assert row["quotedCount"] == 1 and row["providerId"] == "prov-1"
    assert row["phone"] == "(615) 555-0101"
    assert find_partners("Painting", "37203") == [] or all(
        r["name"] != "Brightline Painting" for r in find_partners("Painting", "37203")
    )
    assert find_prospects("Painting", "37203")[0]["name"] == "Brightline Painting"


def test_a_second_quote_bumps_the_count_and_widens_coverage(tmp_path):
    record_quoted_provider("Brightline Painting", "Painting", "37203")
    row = record_quoted_provider("brightline painting", "Flooring", "37212")
    assert row["quotedCount"] == 2
    assert row["serviceTypes"] == ["Painting", "Flooring"]
    assert row["zips"] == ["37203", "37212"]
    assert len([r for r in _rows(tmp_path) if "brightline" in r["name"].lower()]) == 1


def test_an_existing_partner_keeps_its_status_when_it_quotes(tmp_path):
    (tmp_path / "partners.json").write_text(json.dumps([
        {"name": "Partner Painting Co.", "serviceTypes": ["Painting"], "zips": ["37203"]}
    ]), encoding="utf-8")
    row = record_quoted_provider("Partner Painting Co.", "Painting", "37203")
    assert relationship(row) == "partner" and row["quotedCount"] == 1
    assert find_partners("Painting", "37203")[0]["name"] == "Partner Painting Co."


def test_recording_never_raises_on_a_bad_name():
    assert record_quoted_provider("", "Painting", "37203") is None
    assert record_quoted_provider(None, "Painting", "37203") is None


@pytest.mark.asyncio
async def test_the_ops_upload_api_records_the_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ops_token", "ops-secret")
    await quote_store.save(_record())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        resp = await http.post(
            "/api/v1/ops/quote-requests/qr_supply01/quotes",
            json={"quotes": [
                {"providerName": "Nash Painting", "priceUsd": 1850},
                {"providerName": "Demo Estimate", "priceUsd": 1000, "isEstimate": True},
            ]},
            headers={"Authorization": "Bearer ops-secret"},
        )
    assert resp.status_code == 200
    names = [r["name"] for r in _rows(tmp_path)]
    assert "Nash Painting" in names
    assert "Demo Estimate" not in names          # illustrative estimates are not providers


# ------------------------------------------------------------- the email
def test_the_email_separates_partners_from_previous_quoters(tmp_path):
    (tmp_path / "partners.json").write_text(json.dumps([
        {"name": "Partner Painting Co.", "serviceTypes": ["Painting"], "zips": ["37203"],
         "contactName": "Sam"},
    ]), encoding="utf-8")
    record_quoted_provider("Nash Painting", "Painting", "37203", contact={"phone": "(615) 555-0102"})
    import_prospects([{"name": "Sheet Painters", "serviceTypes": ["Painting"], "zips": ["37203"]}],
                     source="sheet:providers")

    partners = find_partners("Painting", "37203")
    prospects = find_prospects("Painting", "37203")
    assert [p["name"] for p in partners] == ["Partner Painting Co."]
    assert [p["name"] for p in prospects] == ["Nash Painting", "Sheet Painters"]

    subject, body = build_ops_email(_record(), partners, None, prospects)
    assert "NO PROVIDER COVERAGE" not in subject
    partner_block = body.split("TakeShape partners for this area")[1].split("Not partners")[0]
    assert "Partner Painting Co." in partner_block and "Nash Painting" not in partner_block
    assert "Nash Painting | quoted 1x through TakeShape" in body
    assert "Sheet Painters | prospect from sheet:providers" in body

    html = build_ops_email_html(_record(), partners, None, prospects)
    assert "not a partner" in html and "Nash Painting" in html


def test_an_empty_list_is_said_out_loud(tmp_path):
    record = _record(serviceType="Roofing", zip="99999")
    partners = find_partners("Roofing", "99999")
    prospects = find_prospects("Roofing", "99999")
    assert coverage_gap(partners, prospects, None)

    subject, body = build_ops_email(record, partners, None, prospects)
    assert subject.startswith("[NO PROVIDER COVERAGE]")
    assert "NO PROVIDER COVERAGE for Roofing in 99999" in body
    assert "manual sourcing" in body
    html = build_ops_email_html(record, partners, None, prospects)
    assert "NO PROVIDER COVERAGE for Roofing in 99999" in html


def test_one_researched_company_is_enough_to_clear_the_gap():
    assert not coverage_gap([], [], [{"name": "Found Online LLC"}])
    subject, _ = build_ops_email(_record(), [], [{"name": "Found Online LLC"}], [])
    assert "NO PROVIDER COVERAGE" not in subject
