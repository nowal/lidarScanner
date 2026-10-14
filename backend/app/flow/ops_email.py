"""Lead-package email to operations (provider-finder scope, agreed Sep 1).

On quote-request submission the full lead package is emailed to
``LIDARAI_OPS_EMAIL`` (Quintin in production; a test inbox during
development): homeowner contact, project synopsis, measurements, the 3D
model link, suggested providers (every company in the provider table that
serves the trade and zip plus web-researched leads, ranked by review and
social presence with each row's tag and score explained -- see
docs/adr/provider-ranking.md; the older partners-first grouping is behind
the deprecated LIDARAI_PREFERRED_PARTNER_ORDERING_ENABLED flag), and a
signed link where ops enters the checked quote — which flows straight back
to the app on its next poll.

Privacy (SOW §12): the street address is NEVER in this email — it is
released through the ops API only after the homeowner selects a quote.

Transport: plain SMTP (``LIDARAI_SMTP_*``). When SMTP is unconfigured the
composed email is written to ``{storage_dir}/ops_outbox/{id}.json`` instead
— dev/test builds capture exactly what would have been sent, and delivery
failures never block the homeowner's submission.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import smtplib
import time
from email.message import EmailMessage
from email.utils import getaddresses
import re
from pathlib import Path
from typing import Any

from ..config import settings

logger = logging.getLogger("lidarai.flow.ops_email")

ENTRY_LINK_TTL_SECONDS = 14 * 24 * 3600


# --------------------------------------------------------------------------
# Signed quote-entry links (no ops token in email — the link authorizes one
# request id, expires, and grants only view-lead + enter-quote)
# --------------------------------------------------------------------------
def _secret() -> str:
    return settings.flow_token_secret or settings.auth_token or "dev-secret"


def entry_signature(request_id: str, exp: int) -> str:
    payload = f"{request_id}.{exp}".encode()
    return hmac.new(_secret().encode(), payload, hashlib.sha256).hexdigest()


def verify_entry_signature(request_id: str, exp: int, sig: str) -> bool:
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(entry_signature(request_id, exp), sig or "")


def entry_link(request_id: str) -> str | None:
    # exp and sig ride in the PATH, not the query string: email transports
    # apply quoted-printable encoding that can mangle "=" characters, which
    # silently breaks query-param links (observed with Resend → Gmail).
    base = settings.public_base_url.rstrip("/")
    if not base:
        return None
    exp = int(time.time()) + ENTRY_LINK_TTL_SECONDS
    sig = entry_signature(request_id, exp)
    return f"{base}{settings.api_prefix}/ops/entry/{request_id}/{exp}/{sig}"


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------
def _fmt_measurements(measurements: dict[str, Any]) -> list[str]:
    lines = []
    if measurements.get("capture"):
        lines.append(f"Capture: {measurements['capture']}")
    extent = measurements.get("meshExtentFeet") or {}
    if extent:
        lines.append(
            f"Scan extent (LiDAR mesh, approx.): {extent.get('width')} x {extent.get('length')} ft, "
            f"{extent.get('height')} ft tall (the whole area walked)"
        )
    scanned = measurements.get("scannedSurfacesSquareFeet") or {}
    if scanned:
        parts = []
        if scanned.get("upright"):
            high = f", up to {scanned['heightFeet']} ft high" if scanned.get("heightFeet") else ""
            parts.append(f"upright (walls, siding, fences) ~{float(scanned['upright']):,.0f} sq ft{high}")
        if scanned.get("ground"):
            parts.append(f"level ground (drive, walks, patio, any lawn scanned) ~{float(scanned['ground']):,.0f} sq ft")
        lines.append("Scanned surfaces (LiDAR mesh, approx.): " + "; ".join(parts))
        lines.append("  (what the walk covered; anything it did not pass or above ~15 ft is not included)")
    area = measurements.get("floorAreaSquareFeet")
    if area:
        label = "Room area" if measurements.get("room") else "Floor area"
        lines.append(f"{label}: ~{float(area):,.0f} sq ft")
    # RoomPlan counts window surfaces, so a bank of three sashes is one.
    # Labelled as openings so a painter does not price trim for two windows
    # when there are eight (Quintin, Sep 23).
    for key, label in (("windowCount", "Window openings (scan)"), ("doorCount", "Doors")):
        if measurements.get(key):
            lines.append(f"{label}: {measurements[key]}")
    openings = measurements.get("windowOpenings") or []
    if openings:
        sizes = ", ".join(f"{o['widthFeet']} x {o['heightFeet']} ft" for o in openings[:12])
        lines.append(f"  Opening sizes (w x h): {sizes}")
        lines.append("  (a bank of several sashes reads as one opening; confirm the sash count)")
    if measurements.get("homeownerSupplied"):
        lines.append("From the homeowner, in their words: " + "; ".join(measurements["homeownerSupplied"]))
    if measurements.get("fixtures"):
        lines.append("In the room: " + ", ".join(measurements["fixtures"]))
    rooms = measurements.get("rooms") or []
    for room in rooms[:6]:
        if isinstance(room, dict) and room.get("name"):
            detail = f"  - {room['name']}"
            if room.get("floorAreaSquareFeet"):
                detail += f" (~{float(room['floorAreaSquareFeet']):,.0f} sq ft)"
            lines.append(detail)
    for key in ("wallCount", "windowCount", "openingCount"):
        if measurements.get(key):
            lines.append(f"{key}: {measurements[key]}")
    return lines or ["No measurements attached."]


RANKED_HEADER = "Ranked by review and social presence (partners tagged; scores explained per row):"
RANKED_ATTRIBUTION = (
    "Ratings and review counts via Google Places. Yelp, Facebook, Instagram and "
    "Nextdoor are profile links only -- never scraped; a missing number is unknown, not zero."
)


def _ranked_or_none(ranked: list[dict] | None) -> list[dict] | None:
    """The ranked list is used unless the DEPRECATED partner-first ordering
    flag is on, in which case the grouped section below renders as before."""
    if not ranked or settings.preferred_partner_ordering_enabled:
        return None
    return ranked


def relationship_tag(entry: dict) -> str:
    """The word "partner" is reserved: only a row TakeShape entered as a
    partner is ever called one, whatever its score."""
    row = entry.get("row") or {}
    rel = entry.get("relationship") or row.get("relationship")
    if rel == "partner":
        return "TakeShape partner"
    if rel == "quoted" or row.get("quotedCount"):
        return f"quoted {int(row.get('quotedCount') or 1)}x through TakeShape"
    if rel == "researched":
        return "found online, unvetted"
    return f"prospect from {row.get('source') or 'the provider list'}"


PLATFORM_LABELS = {
    "google": "Google", "yelp": "Yelp", "facebook": "Facebook",
    "instagram": "Instagram", "nextdoor": "Nextdoor",
}
PAST_QUOTER_MARK = "PAST QUOTER"


def _metric_text(component: dict) -> str | None:
    """"4.8 stars, 412 reviews" from a presence component -- only fields the
    source actually reported. A null is never shown, and never shown as 0;
    a real 0 from a recorded source is a real number."""
    if not component.get("source"):
        return None
    bits: list[str] = []
    rating = component.get("rating")
    if rating is not None:
        bits.append(f"{float(rating):.1f} stars")
    if component.get("reviewCount") is not None:
        bits.append(f"{int(component['reviewCount'])} reviews")
    elif component.get("followerCount") is not None:
        bits.append(f"{int(component['followerCount'])} followers")
    return ", ".join(bits) or None


def provider_row_view(entry: dict) -> dict:
    """Everything one ranked row shows, computed once so the plain-text and
    HTML parts carry the same content (links included)."""
    from .provider_ranking import PLATFORMS

    row = entry.get("row") or {}
    comps = entry.get("components") or {}
    platforms = comps.get("platforms") or {}
    ordered = [p for p in PLATFORMS if p in platforms] + sorted(p for p in platforms if p not in PLATFORMS)
    presences = []
    for platform in ordered:
        c = platforms[platform]
        presences.append(
            {
                "platform": platform,
                "label": PLATFORM_LABELS.get(platform, platform.title()),
                "url": c.get("profileUrl") or None,
                "metric": _metric_text(c),
                "verified": str(c.get("lastVerifiedAt") or "")[:10] or None,
                "source": c.get("source") or None,
            }
        )
    notes: list[str] = []
    if comps.get("multiPlatformBonus"):
        notes.append(f"multi-platform +{comps['multiPlatformBonus']:.2f}")
    if comps.get("quotedBoost"):
        notes.append(f"quoted boost +{comps['quotedBoost']:.2f}")
    if comps.get("dataCoverage") == "none":
        notes.append("no review or social data on file")
    elif comps.get("dataCoverage") == "links_only":
        notes.append("profile links only, no numbers on file")
    if comps.get("smallSet"):
        notes.append("small local set (<3 candidates)")
    contact = [b for b in (
        f"contact {row['contactName']}" if row.get("contactName") else None,
        row.get("phone"), row.get("email"),
    ) if b]
    return {
        "rank": entry.get("rank", "?"),
        "name": entry.get("name", ""),
        "tag": relationship_tag(entry),
        # From the fact, not the tunable weight: LIDARAI_RANK_QUOTED_BOOST=0
        # must not erase the mark (review, Sep 10).
        "pastQuoter": int(comps.get("quotedCount") or row.get("quotedCount") or 0) > 0,
        "quotedCount": int(comps.get("quotedCount") or row.get("quotedCount") or 0),
        "relationship": entry.get("relationship"),
        "contact": [str(b) for b in contact],
        "website": str(row["website"]) if row.get("website") else None,
        "score": float(entry.get("score") or 0),
        # A score is only worth printing when at least one platform actually
        # contributed a number. With discovery off every row lands on the
        # no-data baseline, and a column of identical "0.00"s reads as a
        # verdict on the companies rather than as an absence of data.
        "scored": bool(comps.get("scoredPlatforms")),
        "presences": presences,
        "notes": notes,
        "sample": bool(row.get("sample")),
    }


def _ranked_lines(ranked: list[dict]) -> list[str]:
    lines = [RANKED_HEADER]
    for entry in ranked:
        v = provider_row_view(entry)
        head = f"{v['rank']}. "
        if v["pastQuoter"]:
            head += f"[{PAST_QUOTER_MARK} x{v['quotedCount']}] "
        bits = [
            head + v["name"], v["tag"], *v["contact"], v["website"],
            f"score {v['score']:.2f}" if v["scored"] else None,
        ]
        lines.append("  " + " | ".join(b for b in bits if b))
        for p in v["presences"]:
            detail = p["metric"] or "profile link only"
            if p["verified"]:
                detail += f" (verified {p['verified']})"
            if p["url"]:
                detail += f" {p['url']}"
            lines.append(f"     {p['label']}: {detail}")
        if v["notes"]:
            lines.append("     " + "; ".join(v["notes"]))
        if v["sample"]:
            lines.append("     (sample row -- replace with the real partner table)")
    lines.append(RANKED_ATTRIBUTION)
    return lines


def _provider_section(
    partners: list[dict],
    researched: list[dict] | None,
    prospects: list[dict] | None = None,
    gap_note: str | None = None,
    ranked: list[dict] | None = None,
) -> list[str]:
    ranked = _ranked_or_none(ranked)
    if ranked:
        return _ranked_lines(ranked)
    lines: list[str] = []
    if partners:
        lines.append("TakeShape partners for this area (contact first):")
        for p in partners:
            bits = [p.get("name", "")]
            if p.get("contactName"):
                bits.append(f"contact {p['contactName']}")
            if p.get("phone"):
                bits.append(p["phone"])
            if p.get("email"):
                bits.append(p["email"])
            if p.get("ratingLabel"):
                bits.append(p["ratingLabel"])
            lines.append("  - " + " | ".join(b for b in bits if b))
            if p.get("sample"):
                lines.append("    (sample row — the real partner table lands with TakeShape Supabase access)")
    if prospects:
        lines.append("Not partners, but known to us (previous quoters first):")
        for p in prospects:
            bits = [p.get("name", "")]
            if p.get("relationship") == "quoted" or p.get("quotedCount"):
                bits.append(f"quoted {int(p.get('quotedCount') or 1)}x through TakeShape")
            else:
                bits.append(f"prospect from {p.get('source') or 'the provider list'}")
            for key in ("contactName", "phone", "email", "website"):
                if p.get(key):
                    bits.append(str(p[key]))
            lines.append("  - " + " | ".join(b for b in bits if b))
    if researched:
        lines.append("Researched nearby companies (found online, unvetted):")
        for p in researched:
            bits = [p.get("name", "")]
            if p.get("phone"):
                bits.append(p["phone"])
            if p.get("website"):
                bits.append(p["website"])
            if p.get("ratingLabel"):
                bits.append(p["ratingLabel"])
            lines.append("  - " + " | ".join(b for b in bits if b))
            if p.get("note"):
                lines.append(f"    {p['note']}")
    if not lines:
        lines.append(gap_note or "No partner match and provider research is off — source manually.")
    return lines


def coverage_gap_note(record: Any) -> str:
    service = getattr(record, "serviceType", None) or "this trade"
    where = getattr(record, "zip", None) or "this zip"
    return (
        f"NO PROVIDER COVERAGE for {service} in {where}: no partner, no company that has "
        "quoted before, and no researched company. This lead needs manual sourcing, and "
        "the homeowner's expectations may need resetting."
    )


def _scan_facts(record: Any) -> list[str]:
    rubric = getattr(record, "rubric", None) or {}
    facts = rubric.get("fromScan") or []
    if facts:
        return facts
    measured = {k: v for k, v in (record.measurements or {}).items() if k != "homeownerSupplied"}
    facts = [line.replace("wallCount:", "Walls:").replace("openingCount:", "Openings:")
             for line in _fmt_measurements(measured) if not line.startswith("windowCount:")]
    if measured.get("paintableWallSquareFeet"):
        facts.insert(0, f"Paintable wall area: ~{float(measured['paintableWallSquareFeet']):,.0f} sq ft")
    return facts


def build_ops_email(
    record: Any,
    partners: list[dict],
    researched: list[dict] | None,
    prospects: list[dict] | None = None,
    ranked: list[dict] | None = None,
) -> tuple[str, str]:
    """The provider-facing lead, with the same content in both MIME parts."""
    from ..flow_quotes import scope_label
    from .quote_media import link as media_link

    service = record.serviceType or "Home project"
    where = record.zip or "zip unknown"
    title = f"{service} in {where}"
    lines = ["TakeShape", title, f"Submitted {record.createdAt}", "", "HOMEOWNER",
             f"  Name: {record.firstName or '—'}", f"  ZIP code: {record.zip or '—'}",
             "  Address and contact details: withheld until homeowner selects a quote.",
             "", "PROJECT", f"  Scope of work: {scope_label(record)}"]
    if getattr(record, "roomName", None):
        lines.append(f"  Room: {record.roomName}")
    if record.scopeOptions:
        lines.append("  Scope: " + "; ".join(record.scopeOptions))
    if record.materials:
        lines.append("  Materials: " + ", ".join(record.materials))
    lines.extend(["", record.synopsis, "", "FROM THE SCAN"])
    lines.extend("  " + fact for fact in _scan_facts(record))
    media_url = media_link(record)
    if media_url:
        lines.extend(["", "Open scan gallery & 3D viewer", media_url,
                      "Private link expires in 30 days."])
    return f"New quote request: {title}", "\n".join(lines)


def _esc(value) -> str:
    import html as html_mod
    return html_mod.escape(str(value if value is not None else "—"))


def build_ops_email_html(
    record: Any,
    partners: list[dict],
    researched: list[dict] | None,
    prospects: list[dict] | None = None,
    ranked: list[dict] | None = None,
) -> str:
    """Compact, email-client-safe provider brief. No external logo or previews."""
    from ..flow_quotes import scope_label
    from .quote_media import link as media_link
    e = _esc

    def row(label: str, value: Any) -> str:
        return (f'<tr><td style="padding:4px 14px 4px 0;color:#5A6772;font-size:13px;vertical-align:top">{e(label)}</td>'
                f'<td style="padding:4px 0;color:#17212B;font-size:14px">{e(value)}</td></tr>')

    homeowner = row("Name", record.firstName) + row("ZIP code", record.zip)
    homeowner += row("Address & contact", "Withheld until homeowner selects a quote")
    project = row("Scope of work", scope_label(record))
    if getattr(record, "roomName", None):
        project += row("Room", record.roomName)
    if record.scopeOptions:
        project += row("Scope", "; ".join(record.scopeOptions))
    if record.materials:
        project += row("Materials", ", ".join(record.materials))
    facts = "".join(f'<li style="margin:4px 0">{e(fact)}</li>' for fact in _scan_facts(record))
    media_url = media_link(record)
    viewer = (f'<a href="{e(media_url)}" style="display:inline-block;background:#2E7D53;color:#FFFFFF;'
              'border-radius:8px;padding:12px 18px;font-size:14px;font-weight:600;text-decoration:none">'
              'Open scan gallery &amp; 3D viewer</a>'
              '<p style="font-size:12px;color:#8794A0;margin:12px 0 0">Private link expires in 30 days.</p>') if media_url else ""
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#F2F4F6">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F2F4F6;padding:28px 12px"><tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#FFFFFF;border-radius:14px;overflow:hidden;font-family:'Segoe UI',system-ui,-apple-system,sans-serif">
<tr><td style="padding:22px 28px;border-bottom:1px solid #EDF0F3"><span style="font-size:17px;font-weight:700;color:#17212B">TakeShape</span></td></tr>
<tr><td style="padding:24px 28px 8px"><div style="font-size:21px;font-weight:700;color:#17212B;margin-bottom:6px">{e(record.serviceType or "Home project")} in {e(record.zip or "—")}</div>
<div style="font-size:13px;color:#5A6772">Submitted {e(str(record.createdAt)[:16].replace("T", " at "))}</div></td></tr>
<tr><td style="padding:14px 28px 0"><div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#2E7D53;margin-bottom:6px">Homeowner</div><table role="presentation" cellpadding="0" cellspacing="0">{homeowner}</table></td></tr>
<tr><td style="padding:18px 28px 0"><div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#2E7D53;margin-bottom:6px">Project</div><table role="presentation" cellpadding="0" cellspacing="0">{project}</table>
<div style="margin-top:12px;padding:14px;background:#F7F9FA;border-radius:8px;color:#3D4852;font-size:13.5px;line-height:1.55;white-space:pre-line">{e(record.synopsis)}</div>
<div style="margin-top:18px;color:#17212B;font-size:14px;font-weight:600">From the scan</div><ul style="padding-left:18px;color:#5A6772;font-size:13px;line-height:1.5">{facts}</ul></td></tr>
<tr><td style="padding:18px 28px 28px">{viewer}</td></tr></table></td></tr></table></body></html>"""


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------
def reply_to_address() -> str:
    """Where a lead email invites ops to reply.

    Normally the ops address itself: ops replies, the message is
    self-addressed, and the poller reads that same mailbox. When
    ``LIDARAI_OPS_REPLY_TO`` is set the reply goes there instead — the way
    to keep reply-by-email working when the ops mailbox cannot be polled
    (a Workspace account without an app password). The sender check in
    ``ops_reply`` still requires the message to come *from* ops, so this
    changes the destination, never who is trusted."""
    return (settings.ops_reply_to or settings.ops_email).strip()


def _smtp_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_username and settings.smtp_password)


def recipient_list(value: str | list[str]) -> list[str]:
    """Parse headers into actual mailbox lists and reject header injection."""
    raw = ','.join(value) if isinstance(value, list) else value
    if '\r' in raw or '\n' in raw:
        raise ValueError('Invalid email recipient')
    if not raw.strip():
        # An empty header (no cc) parses as ('', '') on Python 3.12; asking
        # for no recipients is not a bad recipient. A header that HAS text
        # but no mailbox ("Ops <>", ", ,") is still rejected below, and so
        # is a list where only some entries resolve.
        return []
    result = []
    for _, addr in getaddresses([raw]):
        if not re.fullmatch(r"[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+", addr):
            raise ValueError('Invalid email recipient')
        if addr.casefold() not in {a.casefold() for a in result}: result.append(addr)
    return result


async def _send_via_resend(to: str | list[str], subject: str, body: str, html: str | None = None,
                           *, cc: list[str] | None = None, idempotency_key: str | None = None) -> None:
    import httpx
    recipients = recipient_list(to)
    copies = [a for a in recipient_list(cc or []) if a.casefold() not in {t.casefold() for t in recipients}]
    payload = {"from": settings.ops_email_from or "TakeShape Ops <onboarding@resend.dev>",
               "to": recipients, "subject": subject, "text": body}
    if copies: payload['cc'] = copies
    if html: payload['html'] = html
    if settings.ops_reply_enabled: payload['reply_to'] = recipient_list(reply_to_address())
    headers = {"Authorization": f"Bearer {settings.resend_api_key}"}
    if idempotency_key: headers['Idempotency-Key'] = idempotency_key
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post('https://api.resend.com/emails', headers=headers, json=payload)
        resp.raise_for_status()


def _send_smtp(to: str | list[str], subject: str, body: str, html: str | None = None,
               *, cc: list[str] | None = None, idempotency_key: str | None = None) -> None:
    recipients = recipient_list(to)
    copies = [a for a in recipient_list(cc or []) if a.casefold() not in {t.casefold() for t in recipients}]
    msg = EmailMessage()
    msg['From'] = settings.ops_email_from or settings.smtp_username
    msg['To'] = ', '.join(recipients)
    if copies: msg['Cc'] = ', '.join(copies)
    if idempotency_key:
        msg['Message-ID'] = f'<{hashlib.sha256(idempotency_key.encode()).hexdigest()}@takeshapehome.com>'
    msg['Subject'] = subject
    if settings.ops_reply_enabled: msg['Reply-To'] = reply_to_address()
    msg.set_content(body)
    if html: msg.add_alternative(html, subtype='html')
    transport = smtplib.SMTP_SSL if settings.smtp_port == 465 else smtplib.SMTP
    with transport(settings.smtp_host, settings.smtp_port, timeout=20) as server:
        if settings.smtp_port != 465 and settings.smtp_starttls: server.starttls()
        server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg, to_addrs=recipients + copies)


def _write_outbox(record_id: str, to: str | list[str], subject: str, body: str, html: str | None = None,
                  *, cc: list[str] | None = None) -> Path:
    outbox = Path(settings.storage_dir) / "ops_outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{record_id}.json"
    path.write_text(json.dumps({"to": to, 'toRecipients': recipient_list(to), 'cc': cc or [],
        "subject": subject, "body": body, "html": html, "composedAt": time.time()}, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


async def send_ops_message(subject: str, body: str, html: str | None = None, *, outbox_key: str = "message",
                           cc: list[str] | None = None, idempotency_key: str | None = None,
                           to: str | list[str] | None = None) -> str:
    """Other operations emails opt into no copies. Recipient lists never grant trust."""
    to = to or settings.ops_email
    if not to: return 'disabled'
    try:
        extras = {}
        if cc: extras['cc'] = cc
        if idempotency_key: extras['idempotency_key'] = idempotency_key
        if settings.resend_api_key:
            await _send_via_resend(to, subject, body, html, **extras)
            return 'sent'
        if _smtp_configured():
            await asyncio.to_thread(_send_smtp, to, subject, body, html, **extras)
            return 'sent'
        _write_outbox(outbox_key, to, subject, body, html, cc=cc)
        return 'outbox'
    except Exception as exc:
        logger.warning("Ops message delivery failed (%s)", type(exc).__name__)
        return 'failed'


async def send_decision_email(record: Any, quote: Any) -> str:
    """Tell operations what the homeowner decided about one quote.

    Declining matters as much as approving: the comment that comes with it
    is the thing that lets operations go back to the provider or find
    another one. The address goes in only on an approval, because that is
    the release moment (SOW §12) and a decline releases nothing."""
    approved = quote.decision == "approved"
    price = _decision_price(quote)
    subject = (
        f"{'APPROVED' if approved else 'DECLINED'}: {quote.providerName}"
        f"{f' · {price}' if price else ''} — {record.id}"
    )
    lines = [
        f"The homeowner {'approved' if approved else 'declined'} a quote on {record.id}.",
        "",
        f"  Provider: {quote.providerName}",
    ]
    if price:
        lines.append(f"  Price: {price}")
    if quote.decisionNote:
        lines.append("")
        lines.append("  They said:")
        lines.extend(f"    {ln}" for ln in quote.decisionNote.splitlines())
    lines.append("")
    if approved:
        lines.append(
            "This is the address-release moment: the street address below goes to "
            "this provider and to nobody else."
        )
        lines.append(f"  Address: {record.address or 'not captured in the walk'}")
        lines.append("")
        if quote.approvalMethod == "callback":
            lines.append(f"  Callback phone: {quote.callbackPhone}")
            lines.append("The homeowner chose a phone call. Share this number only with the selected provider and ask them to call.")
        elif quote.approvalMethod == "signature":
            from .quote_approvals import document_link
            lines.append("The homeowner signed the provider's PDF. Share the signed copy only with this provider.")
            signed_link = document_link(record, quote)
            if signed_link:
                lines.append(f"  Signed quote PDF: {signed_link}")
                lines.append("  Private signed-copy link expires in 30 days.")
        else:
            lines.append("The homeowner selected this provider. Arrange the next step with them.")
    else:
        other = [q for q in record.quotes if q.id != quote.id and q.decision != "declined"]
        lines.append(
            "No address was released. "
            + (
                f"{len(other)} other quote(s) on this request are still open."
                if other
                else "Nothing else is on this request, so it needs another quote."
            )
        )
    return await send_ops_message(subject, "\n".join(lines), outbox_key=f"decision-{record.id}")


def _decision_price(quote: Any) -> str:
    if getattr(quote, "priceUsd", None) is not None:
        return f"${quote.priceUsd:,.0f}"
    low, high = getattr(quote, "priceLowUsd", None), getattr(quote, "priceHighUsd", None)
    if low is not None and high is not None:
        return f"${low:,.0f}-${high:,.0f}"
    return ""


# --------------------------------------------------------------------------
# Outbound queue
#
# The lead email runs provider research that can take a minute or two, so it
# must not sit on the homeowner's submit. The first attempt at that spawned
# a task inside the request handler — and those die with the request's
# context (observed: a submission through the demo proxy logged
# "ops_email=scheduled" and then nothing at all, losing the lead silently).
# A queue drained by a worker owned by application startup has no such tie.
# --------------------------------------------------------------------------
_queue: "asyncio.Queue[Any] | None" = None


def queue_ops_email(record: Any) -> bool:
    """Hand a lead package to the worker. False when email is switched off."""
    if not settings.ops_email:
        return False
    if _queue is None:
        logger.warning("Ops email worker is not running; sending inline for %s", record.id)
        asyncio.get_event_loop().create_task(send_ops_email(record))
        return True
    _queue.put_nowait(record)
    return True


async def requeue_undelivered() -> int:
    """Put back every lead that was queued and never delivered.

    The queue is in memory: a package that was waiting on provider research
    when a deploy restarted the process was simply gone, and operations never
    heard about the lead. The record carries ``opsEmailQueuedAt`` /
    ``opsEmailDeliveredAt``, durable wherever the quote store is, so the
    worker starts by draining what the last process left behind. Records from
    before those fields existed have neither stamp and are left alone.
    """
    from ..flow_quotes import quote_store

    if _queue is None:
        return 0
    try:
        records = await quote_store.list("submitted")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read quote requests to requeue lead emails: %s", exc)
        return 0
    count = 0
    for record in records:
        # Older builds stamped a disk-only capture as delivered. A surviving
        # outbox file is evidence of that capture; recover those receipts too.
        capture = Path(settings.storage_dir) / "ops_outbox" / f"{record.id}.json"
        if record.opsEmailDeliveredAt and not record.opsEmailCapturedAt and capture.is_file():
            record.opsEmailCapturedAt = record.opsEmailDeliveredAt
            record.opsEmailDeliveredAt = None
            await quote_store.save(record)
        transport_ready = bool(settings.resend_api_key or _smtp_configured())
        if (record.opsEmailQueuedAt and not record.opsEmailDeliveredAt
                and (transport_ready or not record.opsEmailCapturedAt)):
            _queue.put_nowait(record)
            count += 1
    if count:
        logger.info("Requeued %d undelivered lead email(s) from the last process", count)
    return count


async def run_ops_email_worker() -> None:
    """Started once at application startup; lives as long as the process."""
    global _queue
    _queue = asyncio.Queue()
    logger.info("Ops lead-email worker running")
    await requeue_undelivered()
    while True:
        record = await _queue.get()
        try:
            result = await send_ops_email(record)
            logger.info("Ops lead email for %s: %s", record.id, result)
        except Exception as exc:  # noqa: BLE001 — a bad lead must not kill the worker
            logger.warning("Ops lead email for %s crashed: %s", getattr(record, "id", "?"), exc)
        finally:
            _queue.task_done()


async def _mark_delivered(record: Any) -> None:
    """Stamp the record so a restart does not send this lead twice."""
    from ..flow_quotes import quote_store
    from ..models import now_utc

    try:
        record.opsEmailDeliveredAt = now_utc().isoformat()
        latest = await quote_store.get(record.id)
        if latest and latest.opsEmailDeliveryId == record.opsEmailDeliveryId:
            latest.opsEmailDeliveredAt = record.opsEmailDeliveredAt
            await quote_store.save(latest, require_durable=True)
        elif latest is None:
            await quote_store.save(record, require_durable=True)
        (Path(settings.storage_dir) / "ops_outbox" / f"{record.id}.json").unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not stamp lead email delivery on %s: %s", getattr(record, "id", "?"), exc)


_delivery_locks: dict[str, asyncio.Lock] = {}


async def send_ops_email(record: Any) -> str:
    from ..flow_quotes import quote_store
    lock = _delivery_locks.setdefault(record.id, asyncio.Lock())
    async with lock:
        latest = await quote_store.get(record.id)
        if record.opsEmailDeliveredAt or (latest and latest.opsEmailDeliveredAt):
            return 'already_sent'
        if latest and latest.opsEmailDeliveryId != record.opsEmailDeliveryId:
            return 'superseded'
        if latest and latest.opsEmailPayload:
            record.opsEmailPayload = latest.opsEmailPayload
            record.scanMedia = latest.scanMedia
        return await _deliver_ops_email(record)


async def _deliver_ops_email(record: Any) -> str:
    """Compose and deliver the lead email. Returns 'sent', 'outbox',
    'disabled', or 'failed'. Never raises — delivery problems must not block
    the homeowner's submission (same rule as the webhook)."""
    if not settings.ops_email:
        return "disabled"
    try:
        from . import partners as partner_table
        from .local_research import discover_providers, lookup_provider_leads
        from .partners import coverage_gap, find_partners, find_prospects, rank_for_lead

        from ..flow_quotes import quote_store
        if record.opsEmailPayload:
            return await _deliver_payload(record)
        from .quote_media import prepare_email
        await prepare_email(record)

        # Durable table first: a cold instance must not rank the sample seed.
        await partner_table.rehydrate()
        researched = None
        if settings.provider_finder_enabled and record.serviceType and record.zip:
            researched = await lookup_provider_leads(record.serviceType, record.zip)
        # Discovery (flag-gated) attaches platform presences to the table
        # before the rows are read, so the ranking sees them.
        if settings.provider_discovery_enabled and record.serviceType and record.zip:
            await discover_providers(record.serviceType, record.zip)
        partners = find_partners(record.serviceType, record.zip)
        prospects = find_prospects(record.serviceType, record.zip)
        if coverage_gap(partners, prospects, researched):
            logger.warning(
                "No provider coverage for %s (%s in %s): partner list, previous quoters "
                "and research all came back empty",
                record.id, record.serviceType, record.zip,
            )
        ranked = None
        if settings.preferred_partner_ordering_enabled:
            partner_table._warn_legacy_ordering()
        else:
            ranked = rank_for_lead(record.serviceType, record.zip, researched)
            for entry in ranked:
                logger.info("Ranked provider for %s: #%s %s score=%s components=%s",
                            record.id, entry["rank"], entry["name"], entry["score"],
                            json.dumps(entry["components"], default=str))
        subject, body = build_ops_email(record, partners, researched, prospects, ranked)
        html = build_ops_email_html(record, partners, researched, prospects, ranked)
        record.opsEmailPayload = {'subject': subject, 'body': body, 'html': html,
            'to': settings.ops_email, 'cc': recipient_list(settings.ops_quote_cc)}
        await quote_store.save(record, require_durable=True)
        return await _deliver_payload(record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops lead email for %s failed: %s", record.id, exc)
        return "failed"


async def _deliver_payload(record):
    from ..flow_quotes import quote_store
    from ..models import now_utc
    p = record.opsEmailPayload
    result = await send_ops_message(p['subject'], p['body'], p['html'], outbox_key=record.id,
        to=p['to'], cc=p['cc'], idempotency_key=f'quote/{record.id}/{record.opsEmailDeliveryId}')
    if result == 'sent':
        await _mark_delivered(record)
    elif result == 'outbox':
        record.opsEmailCapturedAt = now_utc().isoformat()
        await quote_store.save(record)
    return result
