"""Provider email privacy, plain-text parity, and exact HTML/text fixtures.

Composition is isolated from real delivery and provider sourcing remains internal.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import httpx
import pytest

import app.flow_quotes as flow_quotes
from app.config import settings
from app.flow import partners
from app.flow.ops_email import (
    PAST_QUOTER_MARK,
    build_ops_email,
    build_ops_email_html,
    provider_row_view,
    send_ops_email,
)
from app.flow.provider_ranking import rank_candidates

BACKEND = Path(__file__).resolve().parents[1]
GOLDEN = BACKEND / "tests" / "fixtures" / "golden"

_spec = importlib.util.spec_from_file_location("ops_email_samples", BACKEND / "scripts" / "ops_email_samples.py")
samples = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(samples)

ADDRESS = "1 Withheld Street, Not In The Email"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "public_base_url", "")
    monkeypatch.setattr(settings, "ops_reply_enabled", False)
    monkeypatch.setattr(settings, "preferred_partner_ordering_enabled", False)
    yield tmp_path


def _compose(name: str, tmp_path: Path):
    return samples.compose(name, tmp_path / name)


def _row_block(text: str, name: str) -> str:
    """One ranked row and its detail lines, from the numbered line that
    names the provider to the line before the next numbered row."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if re.match(r"^\s+(\d+)\. ", l) and name in l)
    block = [lines[start]]
    for line in lines[start + 1:]:
        if re.match(r"^\s+\d+\. ", line) or not line.startswith("       "):
            break
        block.append(line)
    return "\n".join(block)


# The lead now contains only the project, scan facts, and private viewer link.
def test_provider_brief_omits_internal_details_and_contact_information(tmp_path):
    for name in ("urban", "rural", "conflicting"):
        subject, text, html = _compose(name, tmp_path)
        record = samples.sample_record(name)
        for content in (text, html):
            for removed in (record.id, record.contactEmail, record.contactPhone,
                            "PAST QUOTER", "Suggested providers", "Provider fields", "Enter the checked quote",
                            "Download original USDZ", "takeshape-logo", "<img", "Ranked by review"):
                if removed:
                    assert removed not in content
            assert record.firstName in content and record.zip in content
            assert "Submitted" in content
        assert record.id not in subject


def test_provider_metrics_still_distinguish_missing_numbers_from_zero():
    entry = rank_candidates([{"name": "Quiet Brush LLC", "relationship": "prospect", "presences": [
        {"platform": "google", "profileUrl": "https://maps.google.com/?cid=9", "rating": None,
         "reviewCount": None, "source": "google_places_api", "lastVerifiedAt": "2026-09-09T10:00:00+00:00"},
        {"platform": "yelp", "profileUrl": "https://www.yelp.com/biz/quiet-brush", "rating": 4.1,
         "reviewCount": 0, "source": "ops_entry", "lastVerifiedAt": "2026-09-09T10:00:00+00:00"},
    ]}])[0]
    by_platform = {p["platform"]: p for p in provider_row_view(entry)["presences"]}
    assert by_platform["google"]["metric"] is None
    assert by_platform["yelp"]["metric"] == "4.1 stars, 0 reviews"


def test_plain_text_carries_the_same_content_as_html(tmp_path):
    for name in ("urban", "rural", "conflicting"):
        _, text, html = _compose(name, tmp_path)
        for url in re.findall(r'href="([^"]+)"', html):
            assert url in text, f"{name}: {url} is in the HTML but not the text part"
        for entry_name in re.findall(r"<span style=\"font-weight:600\">\d+\. ([^<]+)</span>", html):
            assert entry_name in text
        for metric in re.findall(r"(\d+\.\d stars, \d+ (?:reviews|followers))", html):
            assert metric in text
        assert ("PAST QUOTER" in html) == ("PAST QUOTER" in text)


# ------------------------------------------------------- identifiers (D2)
def test_ops_view_carries_no_thread_homeowner_or_job_identifiers():
    record = samples.sample_record("urban")
    view = record.ops_view()
    flat = json.dumps(view)
    assert "conversation" not in view and "homeownerId" not in view["homeowner"]
    assert "jobId" not in view["model"] and view["model"]["url"].startswith("https://")
    assert "thread-urban-not-in-email" not in flat
    assert "00000000-0000-0000-0000-000000000000" not in flat
    assert "job-not-in-email" not in flat
    assert view["quoteRequestId"] == "qr_sample_urban"
    # A record with no model link at all still views cleanly.
    bare = samples.sample_record("rural")
    bare.modelLink = None
    assert bare.ops_view()["model"] is None


@pytest.mark.asyncio
async def test_the_webhook_payload_carries_none_of_them(monkeypatch):
    monkeypatch.setattr(settings, "ops_webhook_url", "https://hooks.example/lead")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert await flow_quotes.deliver_to_ops(samples.sample_record("urban")) is True
    flat = json.dumps(seen["payload"])
    assert seen["payload"]["leadPackage"]["quoteRequestId"] == "qr_sample_urban"
    for leak in ("thread-urban-not-in-email", "00000000-0000-0000-0000-000000000000", "job-not-in-email",
                 "threadId", "homeownerId", "jobId", ADDRESS):
        assert leak not in flat


def test_the_email_carries_none_of_them_either(tmp_path):
    for name in ("urban", "rural", "conflicting"):
        _, text, html = _compose(name, tmp_path)
        for leak in ("thread-", "not-in-email", "00000000-0000", "job-not", "threadId", "homeownerId", "jobId"):
            assert leak not in text and leak not in html, f"{name}: {leak}"


# -------------------------------------------------- address withholding
@pytest.mark.asyncio
async def test_the_composition_path_never_includes_the_address(monkeypatch, tmp_path):
    """Through send_ops_email itself, not a hand-built record: the outbox
    capture is exactly what a transport would have sent."""
    monkeypatch.setattr(settings, "ops_email", "ops@example.com")
    samples.install_fixture_table(tmp_path)
    record = samples.sample_record("urban")
    record.selectedQuoteId = "q-1"          # the address IS released in ops_view now
    assert record.ops_view()["addressReleased"] is True
    assert await send_ops_email(record) == "outbox"
    captured = json.loads((tmp_path / "ops_outbox" / "qr_sample_urban.json").read_text(encoding="utf-8"))
    for part in (captured["body"], captured["html"], captured["subject"]):
        assert ADDRESS not in part and "Withheld Street" not in part
    assert "withheld until homeowner selects a quote" in captured["body"]
    assert "Withheld until homeowner selects a quote" in captured["html"]
    assert "dana@example.com" not in captured["body"]
    assert "Ranked by review and social presence" not in captured["body"]


# ------------------------------------------------------------ golden files
@pytest.mark.parametrize("name", ["urban", "rural", "conflicting"])
def test_golden_files(name, tmp_path):
    """Regenerate with: .venv/Scripts/python scripts/ops_email_samples.py --golden"""
    subject, text, html = _compose(name, tmp_path)
    got_text = f"Subject: {subject}\n\n{text}\n"
    txt_path = GOLDEN / f"ops_email_{name}.txt"
    html_path = GOLDEN / f"ops_email_{name}.html"
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        txt_path.write_text(got_text, encoding="utf-8")
        html_path.write_text(html, encoding="utf-8")
    assert got_text == txt_path.read_text(encoding="utf-8"), f"plain text differs from {txt_path.name}"
    assert html == html_path.read_text(encoding="utf-8"), f"HTML differs from {html_path.name}"


def test_the_samples_in_docs_match_the_current_rendering(monkeypatch, tmp_path):
    """docs/samples/ is what Quintin reviews; it must not drift from the code."""
    monkeypatch.setattr(settings, "public_base_url", "https://ops.example.com")
    monkeypatch.setattr(settings, "ops_reply_enabled", True)
    for name in ("urban", "rural", "conflicting"):
        subject, text, html = _compose(name, tmp_path)
        on_disk = (BACKEND / "docs" / "samples" / f"ops-email-{name}.txt").read_text(encoding="utf-8")
        strip = lambda s: "\n".join(l for l in s.splitlines() if "/ops/entry/" not in l)  # noqa: E731
        assert strip(on_disk) == strip(f"Subject: {subject}\n\n{text}\n")
        on_disk_html = (BACKEND / "docs" / "samples" / f"ops-email-{name}.html").read_text(encoding="utf-8")
        scrub = lambda s: re.sub(r"/ops/entry/[^\"']+", "/ops/entry/…", s)  # noqa: E731
        assert scrub(on_disk_html) == scrub(html)
        for leak in (ADDRESS, "thread-", "00000000-0000", "job-not", "quintin", "noah"):
            assert leak.lower() not in text.lower() and leak.lower() not in html.lower()
