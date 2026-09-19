"""The provider's estimate travels with the quote (Quintin, Sep 17).

Nathan's question: "is it also possible for him to reply to the email and it
be used to fill out the form (including attaching a pdf invoice)?" The reply
already filled the form; this covers the PDF riding along with it.

PDF only, one per reply, capped. A storage failure costs the document and
never the quote -- the prices in the reply are worth more than the paperwork.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import email
import pytest

from app.flow import ops_reply, supabase_store
from app.flow_quotes import QuoteDocument, ReturnedQuote, quote_store
from app.models import now_utc


def _message(*, attach: bytes | None = None, filename="Estimate #4471.pdf", subtype="pdf"):
    msg = EmailMessage()
    msg["From"] = "quintin@takeshapehome.com"
    msg["Subject"] = "Re: New quote request qr_doc1 - Painting"
    msg.set_content("Brightline Painting came back at $2,450, valid 30 days.")
    if attach is not None:
        msg.add_attachment(
            attach, maintype="application", subtype=subtype, filename=filename
        )
    return email.message_from_bytes(msg.as_bytes())


# ------------------------------------------------------------- extraction
def test_a_pdf_attachment_is_picked_up():
    found = ops_reply._pdf_attachment(_message(attach=b"%PDF-1.4 fake"))
    assert found is not None
    name, data = found
    assert name == "Estimate 4471.pdf", "the name is sanitised for a storage path"
    assert data == b"%PDF-1.4 fake"


def test_a_reply_with_no_attachment_is_fine():
    assert ops_reply._pdf_attachment(_message()) is None


def test_a_non_pdf_attachment_is_ignored():
    msg = _message(attach=b"PK\x03\x04 zip bytes", filename="photos.zip", subtype="zip")
    assert ops_reply._pdf_attachment(msg) is None


def test_an_oversized_attachment_is_skipped_not_fatal():
    huge = b"x" * (ops_reply._MAX_ATTACHMENT_BYTES + 1)
    assert ops_reply._pdf_attachment(_message(attach=huge)) is None


def test_the_attachment_never_eats_the_reply_text():
    """The quote is in the text; reading the attachment must not lose it."""
    msg = _message(attach=b"%PDF-1.4 fake")
    body = ops_reply.strip_quoted_history(ops_reply._plain_text_body(msg))
    assert "Brightline Painting came back at $2,450" in body


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd.pdf", "....etcpasswd.pdf"),
        ("estimate", "estimate.pdf"),
        ("", "estimate.pdf"),
        ("quote for 12 Elm St.PDF", "quote for 12 Elm St.PDF"),
    ],
)
def test_filenames_are_made_safe(raw, expected):
    assert ops_reply._safe_filename(raw) == expected


# ------------------------------------------------------------- attaching
@pytest.mark.asyncio
async def test_the_document_lands_on_the_quote(monkeypatch):
    async def _fake_upload(bucket, object_path, data, content_type):
        assert bucket == "home-assets"
        assert object_path.startswith("flow-quote-docs/qr_doc1/")
        assert content_type == "application/pdf"
        return "https://storage.example/signed/estimate.pdf"

    monkeypatch.setattr(supabase_store, "upload_bytes", _fake_upload)
    quote = ReturnedQuote(id="q1", providerName="Brightline Painting", priceUsd=2450)
    reply = ops_reply.OpsReply(
        "qr_doc1", "quintin@takeshapehome.com", "Re: …", "text",
        attachment=("Estimate.pdf", b"%PDF-1.4 fake"),
    )

    note = await ops_reply._attach_document(reply, [quote])

    assert quote.document is not None
    assert quote.document.fileName == "Estimate.pdf"
    assert quote.document.byteCount == len(b"%PDF-1.4 fake")
    assert quote.document.url == "https://storage.example/signed/estimate.pdf"
    assert "Attached Estimate.pdf to Brightline Painting" in note


@pytest.mark.asyncio
async def test_with_several_quotes_the_confirmation_says_where_it_went(monkeypatch):
    async def _fake_upload(*a, **k):
        return "https://storage.example/signed/x.pdf"

    monkeypatch.setattr(supabase_store, "upload_bytes", _fake_upload)
    quotes = [
        ReturnedQuote(id="q1", providerName="Brightline", priceUsd=2450),
        ReturnedQuote(id="q2", providerName="Harbor", priceUsd=2700),
    ]
    reply = ops_reply.OpsReply(
        "qr_doc1", "q@x", "s", "t", attachment=("Estimate.pdf", b"%PDF")
    )

    note = await ops_reply._attach_document(reply, quotes)

    assert quotes[0].document is not None
    assert quotes[1].document is None
    assert "went on the first quote in your reply" in note


@pytest.mark.asyncio
async def test_a_storage_failure_costs_the_document_not_the_quote(monkeypatch):
    async def _fails(*a, **k):
        return None

    monkeypatch.setattr(supabase_store, "upload_bytes", _fails)
    quote = ReturnedQuote(id="q1", providerName="Brightline", priceUsd=2450)
    reply = ops_reply.OpsReply(
        "qr_doc1", "q@x", "s", "t", attachment=("Estimate.pdf", b"%PDF")
    )

    note = await ops_reply._attach_document(reply, [quote])

    assert quote.document is None
    assert "could not be stored" in note
    assert "prices above went through" in note


@pytest.mark.asyncio
async def test_no_attachment_means_no_note(monkeypatch):
    reply = ops_reply.OpsReply("qr_doc1", "q@x", "s", "t", attachment=None)
    assert await ops_reply._attach_document(reply, []) is None


# ------------------------------------------------------------- the homeowner
def test_the_document_reaches_the_homeowner_view():
    quote = ReturnedQuote(
        providerName="Brightline",
        priceUsd=2450,
        document=QuoteDocument(
            fileName="Estimate.pdf", byteCount=88, url="https://storage.example/x.pdf"
        ),
    )
    doc = quote.homeowner_view()["document"]
    assert doc["fileName"] == "Estimate.pdf"
    assert doc["url"] == "https://storage.example/x.pdf"
    assert doc["byteCount"] == 88


def test_a_quote_with_no_document_says_so_explicitly():
    assert ReturnedQuote(providerName="X", priceUsd=1).homeowner_view()["document"] is None


# --------------------------------------------------------- the lead email
def test_the_lead_email_tells_operations_they_can_attach_a_pdf():
    """The sample is what Quintin reviews, and it is pinned to the code by
    test_ops_email_rendering, so asserting on it asserts on the email."""
    sample = (
        Path(__file__).resolve().parents[1] / "docs" / "samples" / "ops-email-urban.txt"
    ).read_text(encoding="utf-8")
    assert "OR JUST REPLY TO THIS EMAIL" in sample
    assert "Attach the provider's estimate as a PDF" in sample

    html = (
        Path(__file__).resolve().parents[1] / "docs" / "samples" / "ops-email-urban.html"
    ).read_text(encoding="utf-8")
    assert "Attach the provider&#x27;s estimate as a PDF" in html or (
        "Attach the provider's estimate as a PDF" in html
    )
