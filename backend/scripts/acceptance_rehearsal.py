"""Acceptance rehearsal: run the full SOW §2 journey — steps 1 through 10 —
against the live model, live Supabase persistence, and real RoomPlan
geometry, ending with ops uploading quotes and the agent presenting them.

This is the §4 acceptance criterion minus real scan bundles ("the flow runs
end to end ... a quote uploaded by operations comes back to the user through
the agent"), runnable any time as a regression rehearsal. Prints a transcript
and a PASS/FAIL checklist.

Usage (from backend/, ~10 live model turns, a few cents):

    .venv/Scripts/python scripts/acceptance_rehearsal.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LIDARAI_AI_PROVIDER", "anthropic")
os.environ.setdefault("LIDARAI_OPS_TOKEN", "rehearsal-ops-token")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from scripts.roomplan_context import context_from_captured_room  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[3] / "takeshape-mobile" / "LidarAITests" / "captured-room.json"

# (homeowner message, scan processing state at that moment)
TURNS = [
    ("Hi! I'm Dana. This room feels dated and I'd like to freshen it up.", "processing"),
    ("Warm neutrals sound lovely. Could you also check — is my 3D model done yet?", "processing"),
    # Explicit premature-push pressure while processing (SOW §3 spot check):
    # the agent must not take the invitation.
    ("Should I go scan the rest of the house right now so you can see everything?", "processing"),
    ("Sure — my zip code is 37203.", "processing"),
    ("Great, it says my model is ready now! Can I add the kitchen to it too?", "complete"),
    ("Let's do the repaint. I'd want walls and trim done, in a low-VOC warm white.", "complete"),
    ("Yes, let's get quotes. My address is 118 Maple Street, Nashville TN, and I'm at dana@example.com.", "complete"),
]

# Phrasings that would mean the agent invited more scanning while processing.
_PUSH_MARKERS = (
    "go ahead and scan", "scan the rest", "scan another", "capture the rest",
    "capture another", "add the rest", "record the rest", "record another room",
    "great idea", "yes, go", "go for it",
)

RESULTS_TURN = ("Any news on my quotes?", "complete")

CHECKS: list[tuple[str, bool]] = []
QUIET = False


def check(name: str, ok: object) -> None:
    CHECKS.append((name, bool(ok)))
    if not QUIET:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def say(text: str) -> None:
    if not QUIET:
        print(text)


async def run_rehearsal(*, quiet: bool = False, thread_suffix: str = "") -> list[tuple[str, bool]]:
    """Run the full journey once; returns the checklist. Importable for the
    repeat-trial reliability harness."""
    global QUIET
    QUIET = quiet
    CHECKS.clear()
    await _run(thread_suffix)
    return list(CHECKS)


async def _run(thread_suffix: str = "") -> None:
    if not settings.anthropic_api_key:
        print("LIDARAI_ANTHROPIC_API_KEY missing; aborting.")
        sys.exit(1)
    context = context_from_captured_room(FIXTURE)
    headers = {"Authorization": f"Bearer {settings.auth_token}"} if settings.auth_token else {}
    ops_headers = {"Authorization": f"Bearer {settings.ops_token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rehearsal", timeout=180) as http:
        # Steps 1–2: opening. thread_suffix keeps repeat-trial runs on
        # distinct threads (the opening endpoint is idempotent per thread).
        opening_body: dict = {"homeContext": context}
        if thread_suffix:
            opening_body["threadId"] = f"rehearsal-{thread_suffix}"
        opening = (await http.post("/api/v1/ai/home-chat/opening", json=opening_body, headers=headers)).json()
        thread_id, token = opening["threadId"], opening["flow"]["token"]
        say(f"\nAGENT (opening):\n  {opening['message']['content']}\n")
        check("step 1-2: opening delivered, grounded, asks first name",
              "?" in opening["message"]["content"] and {1, 2} <= set(opening["flow"]["completedSteps"]))

        messages: list[dict] = []
        scan_gate_held_while_processing = True
        extension_guidance_seen = False
        any_fallback_turn = bool(opening.get("usedFallback"))
        push_resisted = True
        body = opening

        for text, scan_state in TURNS:
            say(f"HOMEOWNER:\n  {text}\n")
            context["meshSummary"]["photorealStatus"] = "ready" if scan_state == "complete" else "processing"
            resp = await http.post(
                "/api/v1/ai/home-chat",
                json={
                    "threadId": thread_id,
                    "flowToken": token,
                    "message": text,
                    "messages": messages,
                    "homeContext": context,
                    "scanContext": {"processingState": scan_state},
                },
                headers=headers,
            )
            body = resp.json()
            token = body["flow"]["token"]
            gates = body["flow"]["gates"]
            say(f"AGENT ({body['model']}, fallback={body['usedFallback']}):\n  {body['message']['content']}\n")
            say(f"  flow: step={body['flow']['stepName']} slots={body['flow']['slots']} gates={gates}\n")
            if scan_state == "processing" and gates["canPromptAdditionalScan"]:
                scan_gate_held_while_processing = False
            if body.get("usedFallback"):
                any_fallback_turn = True
            lowered = body["message"]["content"].lower()
            if scan_state == "processing" and "scan the rest" in text.lower():
                if any(marker in lowered for marker in _PUSH_MARKERS):
                    push_resisted = False
            if scan_state == "complete" and any(k in lowered for k in ("path", "connected", "same", "start from")):
                extension_guidance_seen = True
            messages.append({"id": body["message"]["id"], "role": "homeowner", "content": text, "createdAt": body["message"]["createdAt"]})
            messages.append(body["message"])

        slots = body["flow"]["slots"]
        check("step 3: design conversation held (no fallback turns, any turn)", not any_fallback_turn)
        check("SOW-3 spot check: direct scan-more invitation declined while processing", push_resisted)
        check("step 4: zip captured", slots["zip"] == "37203")
        check("step 5: scan gate held while processing (server-side)", scan_gate_held_while_processing)
        check("step 6: extension answered with continuity guidance", extension_guidance_seen)
        check("step 7: project + scope + materials captured",
              slots["projectType"] is not None and slots["scopeOptions"] and slots["materials"])
        check("step 8: address captured, never echoed to client", slots["addressCaptured"] and "Maple" not in str(body["flow"]))

        # Step 9: submission
        submitted = await http.post(
            "/api/v1/ai/quote-requests",
            json={"threadId": thread_id, "flowToken": token, "confirm": True, "homeContext": context},
            headers=headers,
        )
        ok9 = submitted.status_code == 201
        qr_id = submitted.json().get("quoteRequestId")
        token = submitted.json().get("flowToken", token)
        check("step 9: quote request accepted, lead package built", ok9)
        if ok9:
            package = (await http.get(f"/api/v1/ops/quote-requests/{qr_id}", headers=ops_headers)).json()
            check("step 9: ops package has synopsis+measurements, address withheld",
                  bool(package["project"]["synopsis"]) and package["address"] is None)
            say(f"  lead package synopsis: {package['project']['synopsis'][:180]}...\n")

            # Step 10: ops uploads quotes; agent presents on next turn
            await http.post(
                f"/api/v1/ops/quote-requests/{qr_id}/quotes",
                json={"quotes": [
                    {"providerName": "Brightline Painting", "priceUsd": 2450,
                     "lineItems": [{"item": "Walls + trim, 2 coats low-VOC", "priceUsd": 2450}],
                     "notes": "Can start week of Sep 21"},
                    {"providerName": "Harbor Coatings", "priceLowUsd": 2100, "priceHighUsd": 2900,
                     "notes": "Range firms up after a site visit"},
                ]},
                headers=ops_headers,
            )
            text, scan_state = RESULTS_TURN
            say(f"HOMEOWNER:\n  {text}\n")
            resp = await http.post(
                "/api/v1/ai/home-chat",
                json={"threadId": thread_id, "flowToken": token, "message": text,
                      "messages": messages, "homeContext": context,
                      "scanContext": {"processingState": scan_state}},
                headers=headers,
            )
            body = resp.json()
            say(f"AGENT:\n  {body['message']['content']}\n")
            lowered = body["message"]["content"].lower()
            check("step 10: agent presents and compares returned quotes",
                  "brightline" in lowered and "harbor" in lowered)
            check("step 10: quote request marked presented",
                  body["flow"]["quoteRequest"]["status"] == "presented"
                  and body["flow"]["quoteRequest"]["quotesReturnedCount"] == 2)

            # Selection releases the address (§12)
            quotes = (await http.get(f"/api/v1/ai/quote-requests/{qr_id}", headers=headers)).json()["quotes"]
            await http.post(f"/api/v1/ai/quote-requests/{qr_id}/select",
                            json={"quoteId": quotes[0]["id"]}, headers=headers)
            released = (await http.get(f"/api/v1/ops/quote-requests/{qr_id}", headers=ops_headers)).json()
            check("SOW-12: address released to ops only after selection", released["address"] is not None)


def main() -> None:
    asyncio.run(run_rehearsal())
    passed = sum(1 for _, ok in CHECKS if ok)
    print("\n" + "=" * 60 + f"\nRESULT: {passed}/{len(CHECKS)} checks passed")
    sys.exit(0 if passed == len(CHECKS) else 1)


if __name__ == "__main__":
    main()
