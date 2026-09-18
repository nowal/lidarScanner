"""Reply-by-email quote entry: history stripping, parse→apply, correction
semantics, and the clarification path. IMAP itself is not exercised (the
mailbox's Seen flag is the processed-state; fetch is thin imaplib)."""

import pytest

import app.flow.ops_reply as ops_reply
import app.flow.supabase_store as supabase_store
from app.config import settings
from app.flow.ops_reply import OpsReply, process_reply, strip_quoted_history
from app.flow_quotes import QuoteRequestRecord, ReturnedQuote, quote_store


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    supabase_store._homeowner_cache.clear()
    yield tmp_path
    supabase_store._homeowner_cache.clear()


def test_strip_quoted_history():
    text = (
        "Nash Painting quoted $1,850 for walls and trim.\n"
        "Valid through October.\n"
        "\n"
        "On Tue, Sep 2, 2026 at 2:12 PM TakeShape Ops wrote:\n"
        "> Quote request qr_a7fbbd4c4733\n"
    )
    kept = strip_quoted_history(text)
    assert "Nash Painting" in kept
    assert "wrote:" not in kept and "qr_a7fbbd4c4733" not in kept


def _record(**overrides) -> QuoteRequestRecord:
    base = dict(
        id="qr_replytest01",
        createdAt="2026-09-02T12:00:00+00:00",
        threadId="t-reply",
        status="submitted",
        serviceType="Painting",
        zip="37203",
        firstName="Dana",
        synopsis="Dana wants painting.",
    )
    base.update(overrides)
    return QuoteRequestRecord(**base)


def _reply(text: str) -> OpsReply:
    return OpsReply(
        request_id="qr_replytest01",
        sender="ops-test@example.com",
        subject="Re: New quote request: Painting in 37203 (Dana) — qr_replytest01",
        text=text,
    )


def _capture_messages(monkeypatch):
    sent = []

    async def fake_send(subject, body, html=None, *, outbox_key="message"):
        sent.append({"subject": subject, "body": body})
        return "sent"

    import app.flow.ops_email as ops_email_mod

    monkeypatch.setattr(ops_email_mod, "send_ops_message", fake_send)
    return sent


@pytest.mark.asyncio
async def test_reply_applies_quote_and_confirms(monkeypatch):
    await quote_store.save(_record())
    sent = _capture_messages(monkeypatch)

    async def fake_parse(reply, service):
        return {
            "understood": True,
            "clarificationNeeded": "",
            "quotes": [
                {
                    "providerName": "Nash Painting",
                    "priceUsd": 1850,
                    "priceLowUsd": None,
                    "priceHighUsd": None,
                    "notes": "Walls and trim, two coats.",
                    "validUntil": "2026-10-31",
                }
            ],
        }

    monkeypatch.setattr(ops_reply, "_parse_reply", fake_parse)
    result = await process_reply(_reply("Nash Painting says $1850, walls and trim, two coats."))
    assert result == "applied"

    record = await quote_store.get("qr_replytest01")
    assert record.status == "quotes_ready"
    assert record.quotes[0].providerName == "Nash Painting"
    assert record.quotes[0].priceUsd == 1850
    assert sent and "Quote sent to the homeowner" in sent[0]["subject"]
    assert "Nash Painting" in sent[0]["body"]


@pytest.mark.asyncio
async def test_restated_quote_replaces_same_provider(monkeypatch):
    record = _record()
    record.quotes.append(ReturnedQuote(providerName="Nash Painting", priceUsd=1850))
    original_id = record.quotes[0].id
    await quote_store.save(record)
    _capture_messages(monkeypatch)

    async def fake_parse(reply, service):
        return {
            "understood": True,
            "clarificationNeeded": "",
            "quotes": [
                {
                    "providerName": "nash painting",
                    "priceUsd": 2000,
                    "priceLowUsd": None,
                    "priceHighUsd": None,
                    "notes": None,
                    "validUntil": None,
                }
            ],
        }

    monkeypatch.setattr(ops_reply, "_parse_reply", fake_parse)
    assert await process_reply(_reply("Correction: Nash said $2,000.")) == "applied"

    updated = await quote_store.get("qr_replytest01")
    assert len(updated.quotes) == 1
    assert updated.quotes[0].priceUsd == 2000
    assert updated.quotes[0].id == original_id  # corrected, not duplicated


@pytest.mark.asyncio
async def test_ambiguous_reply_asks_instead_of_uploading(monkeypatch):
    await quote_store.save(_record())
    sent = _capture_messages(monkeypatch)

    async def fake_parse(reply, service):
        return {
            "understood": False,
            "clarificationNeeded": "Which provider is the $1,850 from?",
            "quotes": [],
        }

    monkeypatch.setattr(ops_reply, "_parse_reply", fake_parse)
    assert await process_reply(_reply("1850 works")) == "clarification"

    record = await quote_store.get("qr_replytest01")
    assert record.status == "submitted" and not record.quotes
    assert sent and "Quick check" in sent[0]["subject"]
    assert "Which provider" in sent[0]["body"]


def test_qr_id_extraction():
    assert ops_reply._QR_ID.search("Re: New quote request … — qr_a7fbbd4c4733").group(0) == (
        "qr_a7fbbd4c4733"
    )
    assert ops_reply._QR_ID.search("no id here") is None



# ------------------------------------------------------- fetch resilience
class _FakeImap:
    """Two quote-request mails in the inbox; the body fetch of the second
    one fails the way Gmail did on Sep 15 ("FETCH => System Error")."""

    def __init__(self, *args, **kwargs):
        self.stamped = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        return "OK", []

    def select(self, box):
        return "OK", []

    def search(self, charset, *criteria):
        return "OK", [b"1 2"]

    def fetch(self, nums, what):
        if what == "(FLAGS)":
            return "OK", [b"1 (FLAGS (\\Seen))", b"2 (FLAGS ())"]
        if nums == b"1":
            raw = (
                b"From: ops@example.com\r\nTo: ops@example.com\r\n"
                b"Subject: Re: New quote request: Painting (Dana) - qr_a7fbbd4c4733\r\n"
                b"Content-Type: text/plain\r\n\r\n"
                b"Harpeth Painting came back at $2400, valid through October.\r\n"
            )
            return "OK", [(b"1 (BODY[] {%d})" % len(raw), raw)]
        import imaplib as _imaplib

        raise _imaplib.IMAP4.error("FETCH => System Error")

    def store(self, num, op, flag):
        self.stamped.append((num, op, flag))
        return "OK", []


def test_a_failed_fetch_keeps_the_replies_already_gathered(monkeypatch):
    fake = _FakeImap()
    monkeypatch.setattr(ops_reply.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    monkeypatch.setattr(settings, "ops_email", "ops@example.com")
    monkeypatch.setattr(settings, "ops_imap_username", "ops@example.com")
    monkeypatch.setattr(settings, "ops_imap_password", "app-password")

    replies = ops_reply.fetch_unseen_replies()

    assert [r.request_id for r in replies] == ["qr_a7fbbd4c4733"]
    assert "Harpeth Painting" in replies[0].text
    # The good message is stamped; the one whose fetch failed is not, so
    # the next cycle retries it instead of losing it.
    assert [n for n, _op, _flag in fake.stamped] == [b"1"]


@pytest.mark.asyncio
async def test_one_crashing_reply_does_not_drop_the_rest(monkeypatch):
    calls = []

    async def flaky(reply):
        calls.append(reply.request_id)
        if reply.request_id == "qr_bad":
            raise RuntimeError("model call exploded")
        return "applied"

    monkeypatch.setattr(ops_reply, "process_reply", flaky)
    results = await ops_reply.process_batch([
        OpsReply("qr_bad", "ops@example.com", "s", "t"),
        OpsReply("qr_good", "ops@example.com", "s", "t"),
    ])
    assert calls == ["qr_bad", "qr_good"]
    assert results == ["failed", "applied"]


def test_an_html_only_reply_is_read():
    """Gmail's API sends HTML-only mail; the reply Nathan sent through it on
    Sep 16 came back as an empty body and was silently dropped."""
    import email as email_mod

    raw = (
        b"From: ops@example.com\r\nTo: ops@example.com\r\n"
        b"Subject: Re: New quote request - qr_a7fbbd4c4733\r\n"
        b"Content-Type: multipart/mixed; boundary=\"b\"\r\n\r\n"
        b"--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<div dir=\"ltr\">Harpeth Painting quoted $2,400 for the kitchen walls.<br>Valid through October 31.</div>"
        b"<div class=\"gmail_quote\">On Mon, Sep 14, 2026 TakeShape Ops &lt;onboarding@resend.dev&gt; wrote:<br>"
        b"<blockquote>Quote request qr_a7fbbd4c4733</blockquote></div>\r\n--b--\r\n"
    )
    msg = email_mod.message_from_bytes(raw)
    text = ops_reply._plain_text_body(msg)
    assert "Harpeth Painting quoted $2,400" in text and "October 31" in text
    kept = ops_reply.strip_quoted_history(text)
    assert "Harpeth" in kept and "qr_a7fbbd4c4733" not in kept
