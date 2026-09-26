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


def build_ops_email(
    record: Any,
    partners: list[dict],
    researched: list[dict] | None,
    prospects: list[dict] | None = None,
    ranked: list[dict] | None = None,
) -> tuple[str, str]:
    """Returns (subject, plain-text body). Text-only on purpose: it reads the
    same everywhere, and ops actions happen through the entry link."""
    service = record.serviceType or "Home project"
    where = record.zip or "zip unknown"
    who = record.firstName or "homeowner"
    room = getattr(record, "roomName", None)
    from .partners import coverage_gap
    from ..flow_quotes import scope_label

    scope = scope_label(record)
    intent = str(getattr(record, "scopeIntent", None) or "undecided")
    if intent == "whole_home":
        subject = f"New quote request: {service} — whole home in {where} ({who}) — {record.id}"
    elif intent == "selected_rooms" and getattr(record, "scopeRooms", None):
        subject = (
            f"New quote request: {service} — {' + '.join(record.scopeRooms)} in {where} "
            f"({who}) — {record.id}"
        )
    elif room:
        subject = f"New quote request: {service} — {room} in {where} ({who}) — {record.id}"
    else:
        subject = f"New quote request: {service} in {where} ({who}) — {record.id}"

    gap = coverage_gap(partners, prospects, researched)
    if gap:
        subject = "[NO PROVIDER COVERAGE] " + subject

    lines: list[str] = []
    lines.append(f"Quote request {record.id} · submitted {record.createdAt}")
    lines.append("")
    lines.append("HOMEOWNER")
    lines.append(f"  Name: {record.firstName or '—'}")
    lines.append(f"  Email: {record.contactEmail or '—'}")
    lines.append(f"  Phone: {record.contactPhone or '—'}")
    lines.append(f"  Zip: {record.zip or '—'}")
    lines.append("  Address: withheld until the homeowner selects a quote (release policy)")
    lines.append("")
    lines.append("PROJECT")
    lines.append(f"  Service: {service}")
    lines.append(f"  Scope of work: {scope}")
    if room:
        lines.append(f"  Room: {room}"
                     + (" (whole-home scan)" if getattr(record, "homeId", None) else ""))
    if record.scopeOptions:
        lines.append("  Scope: " + "; ".join(record.scopeOptions))
    if record.materials:
        lines.append("  Materials: " + ", ".join(record.materials))
    lines.append(f"  Synopsis: {record.synopsis}")
    lines.append("")
    lines.append("MEASUREMENTS")
    lines.extend("  " + l for l in _fmt_measurements(record.measurements))
    caveat = (record.measurements or {}).get("nameCaveat")
    if caveat:
        lines.append(f"  NOTE: {caveat}")
    lines.append("")
    lines.append("3D MODEL")
    model = record.modelLink or {}
    if model.get("url"):
        lines.append(f"  {model['url']}")
        if model.get("note"):
            lines.append(f"  ({model['note']})")
    else:
        lines.append(f"  Not available: {model.get('reason') or model.get('note') or 'no completed scan'}")
    lines.append("")
    lines.append("SUGGESTED PROVIDERS")
    lines.extend("  " + l for l in _provider_section(
        partners, researched, prospects, coverage_gap_note(record) if gap else None, ranked
    ))
    lines.append("")
    link = entry_link(record.id)
    lines.append("ENTER THE CHECKED QUOTE")
    if link:
        lines.append(f"  {link}")
        lines.append("  (link is signed for this request and expires in 14 days; the quote")
        lines.append("  you enter is presented to the homeowner in the app automatically)")
    else:
        lines.append("  Set LIDARAI_PUBLIC_BASE_URL to include a one-click entry link here;")
        lines.append("  until then, upload via the ops API (OPS_GUIDE.md).")
    if settings.ops_reply_enabled:
        lines.append("")
        lines.append("OR JUST REPLY TO THIS EMAIL")
        lines.append("  Write the quote in your own words - it is read automatically and")
        lines.append("  sent to the homeowner, and you get a confirmation of what went out.")
        lines.append("  Attach the provider's estimate as a PDF and it goes to the")
        lines.append("  homeowner with the quote.")
    lines.append("")
    lines.append(
        "The homeowner has been told their request is going out to local "
        "providers and that quotes come back in the app — no exact "
        "turnaround promised, and the people doing the checking are not "
        "named to them. The app shows no price until this quote is entered."
    )
    return subject, "\n".join(lines)


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
    """Email-client-safe HTML alternative (600px card, table layout, inline
    styles). The plain-text part stays canonical; this is presentation."""
    e = _esc
    base = settings.public_base_url.rstrip("/")
    logo = f"{base}{settings.api_prefix}/assets/takeshape-logo.png" if base else ""
    link = entry_link(record.id)
    model_url = (record.modelLink or {}).get("url")

    def row(label: str, value: str) -> str:
        return (
            f'<tr><td style="padding:4px 14px 4px 0;color:#5A6772;font-size:13px;'
            f'white-space:nowrap;vertical-align:top">{e(label)}</td>'
            f'<td style="padding:4px 0;color:#17212B;font-size:14px">{value}</td></tr>'
        )

    partner_rows = ""
    for p in partners:
        contact_bits = " · ".join(
            e(b) for b in (p.get("contactName"), p.get("phone"), p.get("email")) if b
        )
        note = (
            '<div style="color:#8A6D1F;font-size:12px;margin-top:2px">Sample row — the real '
            "partner table lands with TakeShape Supabase access</div>"
            if p.get("sample")
            else ""
        )
        partner_rows += (
            f'<div style="padding:10px 14px;background:#EFF7F1;border-radius:8px;margin:0 0 8px">'
            f'<div style="font-weight:600;color:#17212B;font-size:14px">{e(p.get("name"))}</div>'
            f'<div style="color:#5A6772;font-size:13px">{contact_bits}</div>{note}</div>'
        )

    prospect_rows = ""
    for p in prospects or []:
        tag = (
            f"quoted {int(p.get('quotedCount') or 1)}x through TakeShape"
            if p.get("relationship") == "quoted" or p.get("quotedCount")
            else f"prospect from {p.get('source') or 'the provider list'}"
        )
        contact_bits = " · ".join(
            e(b) for b in (p.get("contactName"), p.get("phone"), p.get("email"), p.get("website")) if b
        )
        prospect_rows += (
            f'<div style="padding:9px 14px;border:1px solid #E4E9ED;border-radius:8px;margin:0 0 8px">'
            f'<div style="font-size:14px;color:#17212B"><span style="font-weight:600">{e(p.get("name"))}</span>'
            f'<span style="color:#8A6D1F"> · {e(tag)} · not a partner</span></div>'
            f'<div style="color:#5A6772;font-size:13px">{contact_bits}</div></div>'
        )

    researched_rows = ""
    for p in researched or []:
        rating = (
            f'<span style="color:#2E7D53;font-weight:600"> · {e(p["ratingLabel"])}</span>'
            if p.get("ratingLabel")
            else ""
        )
        contact_bits = " · ".join(e(b) for b in (p.get("phone"), p.get("website")) if b)
        note = (
            f'<div style="color:#5A6772;font-size:12.5px;margin-top:1px">{e(p["note"])}</div>'
            if p.get("note")
            else ""
        )
        researched_rows += (
            f'<div style="padding:9px 14px;border:1px solid #E4E9ED;border-radius:8px;margin:0 0 8px">'
            f'<div style="font-size:14px;color:#17212B"><span style="font-weight:600">{e(p.get("name"))}</span>{rating}</div>'
            f'<div style="color:#5A6772;font-size:13px">{contact_bits}</div>{note}</div>'
        )
    ranked = _ranked_or_none(ranked)
    if ranked:
        ranked_rows = ""
        for entry in ranked:
            v = provider_row_view(entry)
            tag_color = "#2E7D53" if v["relationship"] == "partner" else "#8A6D1F"
            badge = (
                f'<span style="display:inline-block;background:#2E7D53;color:#FFFFFF;border-radius:4px;'
                f'padding:1px 7px;font-size:11px;font-weight:700;letter-spacing:.04em;margin-right:6px;'
                f'vertical-align:middle">{e(PAST_QUOTER_MARK)} &times;{v["quotedCount"]}</span>'
                if v["pastQuoter"] else ""
            )
            contact_bits = " · ".join(e(b) for b in v["contact"])
            if v["website"]:
                contact_bits += (" · " if contact_bits else "") + (
                    f'<a href="{e(v["website"])}" style="color:#2E7D53">{e(v["website"])}</a>'
                )
            presence_lines = ""
            for p in v["presences"]:
                label = (
                    f'<a href="{e(p["url"])}" style="color:#2E7D53;font-weight:600">{e(p["label"])}</a>'
                    if p["url"] else f'<span style="font-weight:600">{e(p["label"])}</span>'
                )
                detail = e(p["metric"]) if p["metric"] else '<span style="color:#8A939B">profile link only</span>'
                if p["verified"]:
                    detail += f' <span style="color:#8A939B">(verified {e(p["verified"])})</span>'
                url_text = (
                    f' <span style="color:#8A939B;font-size:11.5px;word-break:break-all">{e(p["url"])}</span>'
                    if p["url"] else ""
                )
                presence_lines += (
                    f'<div style="color:#3D4852;font-size:12.5px;margin-top:2px">{label}: {detail}{url_text}</div>'
                )
            notes = (
                f'<div style="color:#8A939B;font-size:12px;margin-top:3px">{e("; ".join(v["notes"]))}</div>'
                if v["notes"] else ""
            )
            sample = (
                '<div style="color:#8A6D1F;font-size:12px;margin-top:2px">Sample row — replace with the real partner table</div>'
                if v["sample"] else ""
            )
            score_span = (
                f'<span style="color:#5A6772"> · score {v["score"]:.2f}</span>'
                if v["scored"] else ""
            )
            ranked_rows += (
                f'<div style="padding:9px 14px;border:1px solid #E4E9ED;border-radius:8px;margin:0 0 8px'
                f'{";background:#EFF7F1" if v["pastQuoter"] else ""}">'
                f'<div style="font-size:14px;color:#17212B">{badge}<span style="font-weight:600">'
                f'{e(v["rank"])}. {e(v["name"])}</span>'
                f'<span style="color:{tag_color}"> · {e(v["tag"])}</span>'
                f'{score_span}</div>'
                f'<div style="color:#5A6772;font-size:13px">{contact_bits}</div>'
                f'{presence_lines}{notes}{sample}</div>'
            )
        ranked_rows += (
            f'<div style="color:#8A939B;font-size:11.5px;margin-top:4px">{e(RANKED_ATTRIBUTION)}</div>'
        )
        partner_rows, prospect_rows, researched_rows = "", "", ranked_rows
    if not partner_rows and not prospect_rows and not researched_rows:
        researched_rows = (
            '<div style="color:#AE432A;font-size:13px;font-weight:600">'
            f"{e(coverage_gap_note(record))}</div>"
        )

    model_block = (
        f'<a href="{e(model_url)}" style="display:inline-block;background:#FFFFFF;color:#2E7D53;'
        f'border:1.5px solid #2E7D53;border-radius:8px;padding:9px 18px;font-size:14px;'
        f'font-weight:600;text-decoration:none">View the 3D model</a>'
        if model_url
        else '<span style="color:#5A6772;font-size:13px">3D model link not available for this request.</span>'
    )
    reply_note = (
        '<p style="color:#5A6772;font-size:13.5px;line-height:1.5;margin:14px 0 0">'
        "Prefer email? <strong>Just reply to this message</strong> with the provider's "
        "quote in your own words — it's read automatically and sent to the homeowner, "
        "and you'll get a confirmation of exactly what went out. "
        "<strong>Attach the provider's estimate as a PDF</strong> and it goes to the "
        "homeowner with the quote.</p>"
        if settings.ops_reply_enabled
        else ""
    )
    logo_img = (
        f'<img src="{e(logo)}" width="36" height="36" alt="TakeShape" '
        f'style="border-radius:8px;vertical-align:middle;margin-right:12px">'
        if logo
        else ""
    )
    from ..flow_quotes import scope_label

    scope = "; ".join(record.scopeOptions) or "—"
    scope_of_work = scope_label(record)
    materials = ", ".join(record.materials) or "—"
    area = (record.measurements or {}).get("floorAreaSquareFeet")
    area_str = f"~{float(area):,.0f} sq ft" if area else "—"

    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#F2F4F6">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F2F4F6;padding:28px 12px">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#FFFFFF;border-radius:14px;overflow:hidden;font-family:'Segoe UI',system-ui,-apple-system,sans-serif">
<tr><td style="padding:22px 28px;border-bottom:1px solid #EDF0F3">
  {logo_img}<span style="font-size:17px;font-weight:700;color:#17212B;vertical-align:middle">TakeShape</span>
  <span style="font-size:13px;color:#5A6772;vertical-align:middle"> · new quote request</span>
</td></tr>
<tr><td style="padding:24px 28px 8px">
  <div style="font-size:19px;font-weight:700;color:#17212B;margin-bottom:2px">{e(record.serviceType or "Home project")} in {e(record.zip or "—")}</div>
  <div style="font-size:13px;color:#5A6772">Request {e(record.id)} · submitted {e(str(record.createdAt)[:16].replace("T", " at "))}</div>
</td></tr>
<tr><td style="padding:14px 28px 0">
  <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#2E7D53;margin-bottom:6px">Homeowner</div>
  <table role="presentation" cellpadding="0" cellspacing="0">
    {row("Name", e(record.firstName))}
    {row("Email", e(record.contactEmail))}
    {row("Phone", e(record.contactPhone))}
    {row("Address", '<span style="color:#8A6D1F">Withheld until the homeowner selects a quote</span>')}
  </table>
</td></tr>
<tr><td style="padding:16px 28px 0">
  <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#2E7D53;margin-bottom:6px">Project</div>
  <table role="presentation" cellpadding="0" cellspacing="0">
    {row("Scope of work", f'<span style="font-weight:600">{e(scope_of_work)}</span>')}
    {row("Room", e(record.roomName) if getattr(record, "roomName", None) else "&mdash;")}
    {row("Scope", e(scope))}
    {row("Materials", e(materials))}
    {row("Floor area", e(area_str))}
  </table>
  <div style="margin-top:10px;padding:12px 14px;background:#F7F9FA;border-radius:8px;color:#3D4852;font-size:13.5px;line-height:1.55">{e(record.synopsis)}</div>
  <div style="margin-top:12px">{model_block}</div>
</td></tr>
<tr><td style="padding:20px 28px 0">
  <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#2E7D53;margin-bottom:8px">Suggested providers</div>
  {partner_rows}{prospect_rows}{researched_rows}
</td></tr>
<tr><td style="padding:22px 28px 26px" align="center">
  {"".join([f'<a href="{e(link)}" style="display:inline-block;background:#2E7D53;color:#FFFFFF;border-radius:9px;padding:13px 30px;font-size:15px;font-weight:700;text-decoration:none">Enter the checked quote</a>']) if link else ""}
  {reply_note}
  <p style="color:#8794A0;font-size:12px;line-height:1.5;margin:16px 0 0">The homeowner has been told their request is going out to local providers and that quotes come back in the app — no exact turnaround promised, and the people doing the checking are not named to them. The app shows no price until a quote is entered. Entry link is signed for this request and expires in 14 days.</p>
</td></tr>
</table>
</td></tr></table>
</body></html>"""


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


async def _send_via_resend(to: str, subject: str, body: str, html: str | None = None) -> None:
    """HTTPS transport (api.resend.com). PaaS hosts (Railway, Render) block
    outbound SMTP ports at the network level, so this is the transport that
    actually works from the deployed demo. An unverified Resend account can
    send only to its own signup address — exactly the test-phase setup; a
    verified domain lifts that for production."""
    import httpx

    payload = {
        "from": settings.ops_email_from or "TakeShape Ops <onboarding@resend.dev>",
        "to": [to],
        "subject": subject,
        "text": body,
    }
    if html:
        payload["html"] = html
    if settings.ops_reply_enabled:
        # Replies must land in the mailbox the reply poller reads, not at
        # the transactional sender.
        payload["reply_to"] = [reply_to_address()]
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json=payload,
        )
        resp.raise_for_status()


def _send_smtp(to: str, subject: str, body: str, html: str | None = None) -> None:
    msg = EmailMessage()
    msg["From"] = settings.ops_email_from or settings.smtp_username
    msg["To"] = to
    msg["Subject"] = subject
    if settings.ops_reply_enabled:
        msg["Reply-To"] = reply_to_address()
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    # Port 465 is implicit TLS from the first byte; 587 upgrades via
    # STARTTLS. Some hosts (Railway included) treat the two differently in
    # their egress policy, so both paths matter.
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
        return
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
        if settings.smtp_starttls:
            server.starttls()
        server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)


def _write_outbox(record_id: str, to: str, subject: str, body: str, html: str | None = None) -> Path:
    outbox = Path(settings.storage_dir) / "ops_outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{record_id}.json"
    path.write_text(
        json.dumps(
            {"to": to, "subject": subject, "body": body, "html": html, "composedAt": time.time()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


async def send_ops_message(
    subject: str, body: str, html: str | None = None, *, outbox_key: str = "message"
) -> str:
    """Deliver an arbitrary message to the ops address over the configured
    transport (Resend > SMTP > outbox capture). Used for lead emails and for
    reply confirmations. Never raises."""
    if not settings.ops_email:
        return "disabled"
    try:
        if settings.resend_api_key:
            await _send_via_resend(settings.ops_email, subject, body, html)
            return "sent"
        if _smtp_configured():
            await asyncio.to_thread(_send_smtp, settings.ops_email, subject, body, html)
            return "sent"
        _write_outbox(outbox_key, settings.ops_email, subject, body, html)
        return "outbox"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops message '%s' failed: %s", subject[:60], exc)
        return "failed"


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
        lines.append(
            "The homeowner has been told the company will reach out directly to "
            "schedule and that nothing is owed before they agree a start date and "
            "a written quote."
        )
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
        await quote_store.save(record)
        (Path(settings.storage_dir) / "ops_outbox" / f"{record.id}.json").unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not stamp lead email delivery on %s: %s", getattr(record, "id", "?"), exc)


async def send_ops_email(record: Any) -> str:
    """Compose and deliver the lead email. Returns 'sent', 'outbox',
    'disabled', or 'failed'. Never raises — delivery problems must not block
    the homeowner's submission (same rule as the webhook)."""
    if not settings.ops_email:
        return "disabled"
    try:
        from . import partners as partner_table
        from .local_research import discover_providers, lookup_provider_leads
        from .partners import coverage_gap, find_partners, find_prospects, rank_for_lead

        if record.homeId:
            # Requeued outbox mail may predate the final upload, and old signed
            # links may have expired. Resolve the current room model at send.
            from ..flow_quotes import build_model_link, quote_store
            from .state import FlowState
            latest = await build_model_link(FlowState(
                thread_id=record.threadId, home_id=record.homeId, active_room_key=record.roomKey))
            if latest.get("url"):
                record.modelLink = latest
                await quote_store.save(record)

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
        result = await send_ops_message(subject, body, html, outbox_key=record.id)
        logger.info("Ops lead email for %s: %s", record.id, result)
        if result == "sent":
            await _mark_delivered(record)
        elif result == "outbox":
            from ..flow_quotes import quote_store
            from ..models import now_utc
            record.opsEmailCapturedAt = now_utc().isoformat()
            await quote_store.save(record)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ops lead email for %s failed: %s", record.id, exc)
        return "failed"
