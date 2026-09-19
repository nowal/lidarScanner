"""Reply-by-email quote entry (equal alternative to the entry page).

Ops replies to the lead email in their own words — "Nash Painting says
$1,850 for walls and trim, valid through October" — and this module turns
that into the same structured quote the entry page produces:

1. A background poller reads the ops mailbox over IMAP (messages whose
   subject carries a ``qr_…`` id). A custom IMAP keyword is the
   processed-state — restarts and redeploys never double-process, and the
   human's read/unread flags are never touched.
2. Only messages *from the configured ops address itself* are processed —
   the lead email sets Reply-To to that mailbox, so an ops reply is
   self-addressed. Mail from anyone else is left alone (defense against
   third parties injecting quotes by guessing a request id).
3. The reply text (quoted history stripped) goes to the model with a
   strict JSON schema. A confident parse is uploaded through the same
   upsert path as the entry page — matching an existing quote by provider
   name, so "actually Nash said $2,000" corrects rather than duplicates —
   and ops gets a confirmation email showing exactly what went out. An
   ambiguous reply gets a clarification email instead, and nothing is
   uploaded.
"""

from __future__ import annotations

import asyncio
import email
import email.header
import imaplib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import anthropic

from ..config import settings

logger = logging.getLogger("lidarai.flow.ops_reply")

_QR_ID = re.compile(r"\bqr_[0-9a-f]{12}\b")

_PARSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["understood", "clarificationNeeded", "quotes"],
    "properties": {
        "understood": {"type": "boolean"},
        # Short question for ops when the reply can't be turned into a
        # quote confidently; empty string when understood.
        "clarificationNeeded": {"type": "string"},
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "providerName",
                    "priceUsd",
                    "priceLowUsd",
                    "priceHighUsd",
                    "notes",
                    "validUntil",
                ],
                "properties": {
                    "providerName": {"type": "string"},
                    "priceUsd": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "priceLowUsd": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "priceHighUsd": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "notes": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "validUntil": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
            },
        },
    },
}


# --------------------------------------------------------------------------
# Reply-text handling
# --------------------------------------------------------------------------
_QUOTE_MARKERS = re.compile(r"(?im)^\s*(?:>|On .{0,120} wrote:|From: |-----Original Message-----)")


def strip_quoted_history(text: str) -> str:
    """Keep only what the ops person actually typed: cut at the first
    quoted-history marker (>-prefixed lines, 'On … wrote:', forwarded
    headers)."""
    lines = []
    for line in text.splitlines():
        if _QUOTE_MARKERS.match(line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = ""
    for data, charset in parts:
        out += data.decode(charset or "utf-8", "replace") if isinstance(data, bytes) else data
    return out


def _plain_text_body(msg: email.message.Message) -> str:
    """The reply as text. Prefers a text/plain part; falls back to the HTML
    part with tags stripped. Mail sent from Gmail's API (and some desktop
    clients) is HTML-only, and an HTML-only reply used to come back as an
    empty body and be dropped without a word (Sep 16)."""
    plain = html = ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get("Content-Disposition") and "attachment" in str(part.get("Content-Disposition")).lower():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text
    return plain or _html_to_text(html)


# A provider's estimate is a page or two; anything larger is not one, and
# the cap keeps a runaway attachment out of memory on the poller thread.
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _pdf_attachment(msg: email.message.Message) -> tuple[str, bytes] | None:
    """The first PDF attached to the reply, if any (Quintin, Sep 17).

    PDF only: it is what providers send and it renders everywhere. One
    only, because a quote has one piece of paperwork and picking between
    several is a guess. Oversized or unreadable parts are skipped with a
    warning rather than failing the reply -- the quote in the text is worth
    more than the attachment."""
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        filename = part.get_filename() or ""
        try:
            filename = str(email.header.make_header(email.header.decode_header(filename)))
        except Exception:  # noqa: BLE001
            pass
        is_pdf = part.get_content_type() == "application/pdf" or filename.lower().endswith(".pdf")
        if not is_pdf:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not decode an attachment on a quote reply: %s", exc)
            continue
        if not payload:
            continue
        if len(payload) > _MAX_ATTACHMENT_BYTES:
            logger.warning(
                "Skipping a %.1f MB attachment on a quote reply (cap is %d MB)",
                len(payload) / 1048576, _MAX_ATTACHMENT_BYTES // 1048576,
            )
            continue
        return (_safe_filename(filename or "estimate.pdf"), payload)
    return None


def _safe_filename(name: str) -> str:
    """A filename safe to put in a storage path and show to a homeowner."""
    name = re.sub(r"[^A-Za-z0-9._ -]", "", name).strip() or "estimate.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:120]


def _html_to_text(html: str) -> str:
    """Tags out, block breaks in, entities decoded -- enough for a human's
    reply to survive; the quoted history below it is stripped afterwards."""
    if not html:
        return ""
    import html as html_mod

    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|blockquote)>", "\n", text)
    text = re.sub(r"(?i)<blockquote[^>]*>", "\n> ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


@dataclass
class OpsReply:
    request_id: str
    sender: str
    subject: str
    text: str
    # The provider's paperwork, when operations attached it (Quintin,
    # Sep 17). (filename, bytes) — never more than one, and PDF only.
    attachment: tuple[str, bytes] | None = None


# --------------------------------------------------------------------------
# IMAP fetch (sync — runs in a thread)
# --------------------------------------------------------------------------
def allowed_reply_senders() -> set[str]:
    """Addresses a quote reply may come from.

    The ops address always counts. ``LIDARAI_OPS_REPLY_SENDERS`` adds
    others, comma-separated — ops with a work account and a personal one
    would otherwise have a reply silently ignored depending on which
    account the phone picked."""
    addresses = {settings.ops_email.strip().lower()}
    addresses.update(
        part.strip().lower() for part in settings.ops_reply_senders.split(",") if part.strip()
    )
    return {a for a in addresses if a}


def _imap_credentials() -> tuple[str, str]:
    return (
        settings.ops_imap_username or settings.smtp_username,
        settings.ops_imap_password or settings.smtp_password,
    )


# Processed-state lives in a custom IMAP keyword, NOT the \Seen flag:
# read/unread in the ops inbox belongs to the human (an auto-read lead
# email is a missed lead). BODY.PEEK keeps fetches from setting \Seen.
_PROCESSED_KEYWORD = "TSOpsProcessed"


def fetch_unseen_replies() -> list[OpsReply]:
    """Fetch not-yet-processed quote-request mail and stamp it with the
    processed keyword. Stamping BEFORE processing means a crash skips a
    message rather than double-posting a quote to a homeowner (the resend /
    entry page covers a skipped one).

    Gmail quirk (verified empirically): custom keywords PERSIST, but
    server-side UNKEYWORD search is unreliable (one stamped message made
    the whole set vanish). So: search broadly, fetch FLAGS, filter
    client-side."""
    import datetime as dt

    user, password = _imap_credentials()
    replies: list[OpsReply] = []
    since = (dt.date.today() - dt.timedelta(days=30)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(settings.ops_imap_host, timeout=30) as imap:
        imap.login(user, password)
        imap.select("INBOX")
        status, data = imap.search(None, 'SUBJECT "quote request"', f"SINCE {since}")
        if status != "OK" or not data or not data[0]:
            return []
        nums = data[0].split()
        status, flag_data = imap.fetch(b",".join(nums), "(FLAGS)")
        if status != "OK":
            return []
        processed_key = _PROCESSED_KEYWORD.encode()
        pending = []
        for entry in flag_data:
            line = entry if isinstance(entry, bytes) else entry[0]
            if not line:
                continue
            num = line.split()[0]
            if processed_key not in line:
                pending.append(num)
        for num in pending:
            # One message at a time, and a failure on one ends the cycle
            # with what was already gathered RETURNED. The first version
            # raised out of this loop, which threw away replies that had
            # just been stamped as processed: Nathan's Sep 15 reply was
            # lost that way to a "FETCH => System Error" on the message
            # after it. A message whose fetch fails is not stamped and is
            # retried next cycle.
            try:
                status, fetched = imap.fetch(num, "(BODY.PEEK[])")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Ops reply fetch failed on message %s; keeping %d gathered reply(ies): %s",
                    num.decode() if isinstance(num, bytes) else num, len(replies), exc,
                )
                break
            if status != "OK" or not fetched or fetched[0] is None:
                continue
            imap.store(num, "+FLAGS", _PROCESSED_KEYWORD)
            msg = email.message_from_bytes(fetched[0][1])
            subject = _decode_header(msg.get("Subject"))
            match = _QR_ID.search(subject) or _QR_ID.search(_plain_text_body(msg)[:2000])
            if not match:
                continue
            sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
            if sender not in allowed_reply_senders():
                logger.info("Ignoring quote-request mail from non-ops sender for %s", match.group(0))
                continue
            body = strip_quoted_history(_plain_text_body(msg))
            if not body:
                logger.warning(
                    "Ops reply for %s had no readable text (content types: %s); skipped",
                    match.group(0), ", ".join(p.get_content_type() for p in msg.walk()),
                )
                continue
            replies.append(
                OpsReply(match.group(0), sender, subject, body[:6000], _pdf_attachment(msg))
            )
    return replies


# --------------------------------------------------------------------------
# Parse + apply
# --------------------------------------------------------------------------
async def _parse_reply(reply: OpsReply, service: str | None) -> dict | None:
    if not settings.anthropic_api_key:
        return None
    try:
        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key, timeout=60.0, max_retries=1
        )
        async with client.messages.stream(
            model=settings.anthropic_model.strip() or "claude-sonnet-5",
            max_tokens=1500,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _PARSE_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "You process an operations email reply about home-service "
                        f"quote request {reply.request_id}"
                        + (f" (a {service} project)" if service else "")
                        + ". Extract the provider quote(s) the ops person states. "
                        "Rules: only extract what is explicitly stated — never invent "
                        "a provider name or a price; a single number is priceUsd, a "
                        "range fills priceLowUsd/priceHighUsd; put scope/timing/"
                        "caveats the homeowner should see in notes; if no usable "
                        "quote is stated, or provider or price is ambiguous, set "
                        "understood=false with a short clarificationNeeded question."
                        "\n\nThe reply:\n" + reply.text
                    ),
                }
            ],
        ) as stream:
            response = await stream.get_final_message()
        if response.stop_reason == "refusal":
            return None
        text = next((b.text for b in response.content if b.type == "text"), None)
        return json.loads(text) if text else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops reply parse failed for %s: %s", reply.request_id, exc)
        return None


def _fmt_price(q: dict[str, Any]) -> str:
    if q.get("priceUsd") is not None:
        return f"${q['priceUsd']:,.0f}"
    return f"${q.get('priceLowUsd', 0):,.0f}-${q.get('priceHighUsd', 0):,.0f}"


async def process_reply(reply: OpsReply) -> str:
    """Returns 'applied', 'clarification', 'unmatched', or 'failed'."""
    from ..flow_quotes import ReturnedQuote, quote_store
    from .ops_email import entry_link, send_ops_message

    record = await quote_store.get(reply.request_id)
    if record is None:
        logger.warning("Ops reply references unknown request %s", reply.request_id)
        return "unmatched"
    if record.status == "closed":
        return "unmatched"

    parsed = await _parse_reply(reply, record.serviceType)
    if parsed is None:
        await send_ops_message(
            f"Couldn't process your reply — {reply.request_id}",
            "Your reply couldn't be processed automatically (a technical "
            "hiccup, not your wording). Nothing was sent to the homeowner.\n\n"
            "You can try replying again, or use the entry page:\n"
            f"{entry_link(reply.request_id) or 'see the original email'}",
            outbox_key=f"{reply.request_id}-reply-error",
        )
        return "failed"

    quotes_in = [q for q in parsed.get("quotes", []) if str(q.get("providerName", "")).strip()]
    if not parsed.get("understood") or not quotes_in:
        question = str(parsed.get("clarificationNeeded") or "").strip() or (
            "I couldn't find a provider name plus a price in the reply."
        )
        await send_ops_message(
            f"Quick check on your quote reply — {reply.request_id}",
            f"Before anything goes to the homeowner: {question}\n\n"
            "Reply to this email with the missing detail, or use the entry "
            f"page:\n{entry_link(reply.request_id) or 'see the original email'}\n\n"
            f"Your reply, as received:\n{reply.text[:1500]}",
            outbox_key=f"{reply.request_id}-clarify",
        )
        return "clarification"

    applied = []
    for q in quotes_in:
        try:
            quote = ReturnedQuote(
                providerName=str(q["providerName"]).strip()[:200],
                priceUsd=q.get("priceUsd"),
                priceLowUsd=q.get("priceLowUsd"),
                priceHighUsd=q.get("priceHighUsd"),
                notes=(str(q["notes"]).strip()[:2000] if q.get("notes") else None),
                validUntil=(str(q["validUntil"]).strip()[:40] if q.get("validUntil") else None),
            )
        except ValueError as exc:
            logger.warning("Ops reply quote invalid for %s: %s", reply.request_id, exc)
            continue
        # Correction semantics: a re-stated quote for the same provider
        # replaces the earlier entry instead of stacking a duplicate.
        existing = next(
            (x for x in record.quotes if x.providerName.strip().lower() == quote.providerName.strip().lower()),
            None,
        )
        if existing is not None:
            quote.id = existing.id
            # A correction keeps paperwork already on file unless this
            # reply brings new paperwork of its own.
            quote.document = existing.document
            record.quotes[record.quotes.index(existing)] = quote
        else:
            record.quotes.append(quote)
        applied.append(quote)

    if not applied:
        return "failed"
    document_note = await _attach_document(reply, applied)
    record.status = "quotes_ready"
    from .partners import note_quoted

    note_quoted(record, applied)
    await quote_store.save(record)
    summary = "\n".join(
        f"  - {q.providerName}: {_fmt_price(q.model_dump())}"
        + (f" — {q.notes}" if q.notes else "")
        for q in applied
    )
    if document_note:
        summary += f"\n{document_note}"
    await send_ops_message(
        f"Quote sent to the homeowner — {reply.request_id}",
        "Processed from your reply and now live in the app:\n\n"
        f"{summary}\n\n"
        "The agent presents it on the homeowner's next chat turn. Spotted a "
        "mistake? Reply again with the correction (same provider name "
        "replaces the earlier quote), or fix it on the entry page:\n"
        f"{entry_link(reply.request_id) or ''}",
        outbox_key=f"{reply.request_id}-confirm",
    )
    logger.info("Ops reply applied %d quote(s) to %s", len(applied), reply.request_id)
    return "applied"


# --------------------------------------------------------------------------
# Poller
# --------------------------------------------------------------------------
def reply_loop_configured() -> bool:
    user, password = _imap_credentials()
    return bool(settings.ops_reply_enabled and settings.ops_email and user and password)


async def poll_forever() -> None:
    logger.info(
        "Ops reply-by-email poller running (every %ss)", settings.ops_reply_poll_seconds
    )
    while True:
        try:
            replies = await asyncio.to_thread(fetch_unseen_replies)
            await process_batch(replies)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ops reply poll cycle failed: %s", exc)
        await asyncio.sleep(max(30, settings.ops_reply_poll_seconds))


async def _attach_document(reply: OpsReply, applied: list) -> str | None:
    """Put the reply's PDF on the quote it belongs to, and say in the
    confirmation where it went.

    One attachment, so it goes on the quote from this reply -- the first
    when the reply carried several, which the confirmation names so
    operations can see it landed where they meant. A storage failure costs
    the document, never the quote."""
    if reply.attachment is None:
        return None
    from . import supabase_store

    filename, data = reply.attachment
    target = applied[0]
    object_path = f"flow-quote-docs/{reply.request_id}/{target.id}.pdf"
    url = await supabase_store.upload_bytes(
        "home-assets", object_path, data, "application/pdf"
    )
    if url is None:
        logger.warning("Quote document for %s could not be stored", reply.request_id)
        return (
            f"  (Your attachment {filename} could not be stored, so it is not "
            "on the quote. The prices above went through.)"
        )
    from ..flow_quotes import QuoteDocument

    target.document = QuoteDocument(
        fileName=filename, byteCount=len(data), url=url
    )
    note = f"  Attached {filename} to {target.providerName}."
    if len(applied) > 1:
        note += " (One attachment, so it went on the first quote in your reply.)"
    return note


async def process_batch(replies: list[OpsReply]) -> list[str]:
    """Process every reply of a cycle; one that blows up does not take the
    others with it (they are already stamped, so a skipped one is gone)."""
    results: list[str] = []
    for reply in replies:
        try:
            result = await process_reply(reply)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ops reply for %s crashed: %s", reply.request_id, exc)
            result = "failed"
        logger.info("Ops reply for %s: %s", reply.request_id, result)
        results.append(result)
    return results
