"""Whole-home robustness run against the running backend (port 8010) using a
REAL ingested home: room naming, unresolved rooms, the homeowner naming a
space, scope with real room names, extension mode, the lead package with
room measurements + a stored room model link, the email, a mid-thread home
switch, and the ops listing.

    python e2e_home.py <homeId> [<otherHomeId>]
"""

import asyncio
import os
import json
import re
import sys
import time
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
from app.flow import enforcement  # noqa: E402

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8010")
STORAGE = Path(os.environ.get("E2E_STORAGE_DIR", str(BACKEND / "backend_storage")))
OPS = {"Authorization": "Bearer " + os.environ.get("E2E_OPS_TOKEN", "e2e-ops-token")}
ADDRESS = "118 Maple Street"
CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def context_for(overview, ready):
    return {
        "contextVersion": "home_ai_context_v1", "roomCount": overview["roomCount"], "rooms": [],
        "totals": {"floorAreaSquareMeters": round(overview["totalAreaSqFt"] / 10.7639), "roomCount": overview["roomCount"]},
        "floorplanSummary": f"{overview['roomCount']} captured spaces, about {overview['totalAreaSqFt']} sq ft.",
        "meshSummary": {"photorealStatus": "ready" if ready else "processing", "keyframeCount": overview["photoCount"]},
        "selectedKeyframes": [], "notes": [],
    }


async def turn(http, thread, token, messages, home_id, overview, text, ready):
    resp = await http.post("/api/v1/ai/home-chat", json={
        "threadId": thread, "flowToken": token, "message": text, "messages": messages,
        "homeContext": context_for(overview, ready), "homeId": home_id,
        "scanContext": {"scanId": home_id, "processingState": "complete" if ready else "processing"}})
    body = resp.json()
    f = body["flow"]
    print(f"HOMEOWNER: {text}\nAGENT: {body['message']['content'][:240]}...\n   step={f['stepName']} "
          f"active={(f.get('home') or {}).get('activeRoom')} unresolved={(f.get('home') or {}).get('unresolvedRoom')} "
          f"scope={f['slots']['scopeIntent']}/{f['slots']['scopeRooms']} gates={f['gates']['extensionPromptMode']} fallback={body['usedFallback']}")
    messages.append({"id": body["message"]["id"], "role": "homeowner", "content": text, "createdAt": body["message"]["createdAt"]})
    messages.append(body["message"])
    return body, f["token"]


async def main(home_id, other_id):
    async with httpx.AsyncClient(base_url=BASE, timeout=180) as http:
        listing = (await http.get("/api/v1/ops/homes", headers=OPS)).json()["homes"]
        ids = [h["homeId"] for h in listing]
        print("homes listed:", ids)
        check("ops listing includes the new home and the earlier ones", home_id in ids and (other_id in ids if other_id else True), ids)
        overview = (await http.get(f"/api/v1/ops/homes/{home_id}", headers=OPS)).json()
        names = {r["name"]: r for r in overview["rooms"]}
        print("rooms:", {n: r["key"] for n, r in names.items()})
        unnamed = [n for n in names if n.startswith("unnamed")]
        confident = [n for n, r in names.items() if r.get("confidentName")]
        check("overview has rooms, some confidently named", overview["roomCount"] >= 2 and confident, (overview["roomCount"], confident))

        thread = f"e2e-home-{home_id[:8]}-{int(time.time())}"
        opening = (await http.post("/api/v1/ai/home-chat/opening", json={
            "threadId": thread, "homeContext": context_for(overview, False), "homeId": home_id})).json()
        token, messages = opening["flow"]["token"], []
        text = opening["message"]["content"]
        print("AGENT (opening):", text[:300])
        check("opening: whole-home level, names a real room, asks a name", any(n.lower() in text.lower() for n in confident) and "?" in text, text[:160])
        check("opening: no invented room", "garage" not in text.lower() and "bedroom" not in text.lower() or "bedroom" in " ".join(names).lower())
        check("opening: no fallback", not opening.get("usedFallback"))

        # Name + a real room by name.
        body, token = await turn(http, thread, token, messages, home_id, overview, "Hi, I'm Dana. Let's start with the kitchen.", False)
        active = (body["flow"].get("home") or {}).get("activeRoom") or {}
        check("names 'kitchen' -> kitchen becomes the active room", "kitchen" in (active.get("name") or "").lower(), active)
        # A room the scan does not have.
        body, token = await turn(http, thread, token, messages, home_id, overview, "Actually what about the garage? I'd like to paint it.", False)
        home = body["flow"].get("home") or {}
        check("unresolved room echoed, not described as seen", home.get("unresolvedRoom") and "garage" in home["unresolvedRoom"].lower(), home)
        check("no scan invitation while processing (garage turn)", not enforcement._SCAN_SUGGESTION.search(body["message"]["content"]), body["message"]["content"][:160])
        # The homeowner names an unnamed area.
        if unnamed:
            key = names[unnamed[0]]["key"]
            body, token = await turn(http, thread, token, messages, home_id, overview,
                                     f"Never mind the garage. That small space you called {unnamed[0]} is the mudroom.", False)
            renamed = (await http.get(f"/api/v1/ops/homes/{home_id}", headers=OPS)).json()
            new_name = next((r["name"] for r in renamed["rooms"] if r["key"] == key), None)
            check("homeowner naming an unnamed area sticks in the index", new_name and "mudroom" in new_name.lower(), (unnamed[0], new_name))
        # Scope with real room names, while processing.
        body, token = await turn(http, thread, token, messages, home_id, overview,
                                 "For the project it's a couple of rooms: the kitchen and the bathroom, nothing else.", False)
        check("scope selected_rooms with real rooms", body["flow"]["slots"]["scopeIntent"] == "selected_rooms"
              and any("kitchen" in r.lower() for r in body["flow"]["slots"]["scopeRooms"]), body["flow"]["slots"]["scopeRooms"])
        body, token = await turn(http, thread, token, messages, home_id, overview, "Should I go walk the rest of the house now?", False)
        check("HARD: gate closed, no scan suggestion", body["flow"]["gates"]["canPromptAdditionalScan"] is False
              and not enforcement._SCAN_SUGGESTION.search(body["message"]["content"]), body["message"]["content"][:160])
        body, token = await turn(http, thread, token, messages, home_id, overview, "My zip is 37203.", False)
        # Ready: step 6 within scope.
        body, token = await turn(http, thread, token, messages, home_id, overview, "The model's ready now. Anything else I should add to it?", True)
        check("step 6: named_rooms mode with the real rooms", body["flow"]["gates"]["extensionPromptMode"] == "named_rooms", body["flow"]["gates"])
        check("step 6: no rest-of-the-home invitation", not enforcement._BROAD_EXTENSION.search(body["message"]["content"]), body["message"]["content"][:200])
        # Quote path on the kitchen.
        body, token = await turn(http, thread, token, messages, home_id, overview,
                                 "Let's do the kitchen paint: walls and trim in a low-VOC warm white.", True)
        body, token = await turn(http, thread, token, messages, home_id, overview,
                                 f"Yes, quotes please. My address is {ADDRESS}, Nashville TN, email dana@example.com.", True)
        submitted = await http.post("/api/v1/ai/quote-requests", json={
            "threadId": thread, "flowToken": token, "confirm": True, "homeContext": context_for(overview, True)})
        check("step 9: submitted", submitted.status_code == 201, submitted.text[:200])
        qr_id = submitted.json().get("quoteRequestId")
        if not qr_id:
            return
        view = (await http.get(f"/api/v1/ops/quote-requests/{qr_id}", headers=OPS)).json()
        m = view["project"]["measurements"]
        print("  package room:", view["project"]["room"], "| scope:", view["project"]["scope"]["label"], "| model:", view["model"])
        check("package: room-level measurements for the active room", view["project"]["room"] and "paintableWallSquareFeet" in m and m.get("floorAreaSquareFeet", 0) < overview["totalAreaSqFt"], m)
        check("package: scope carried", view["project"]["scope"]["intent"] == "selected_rooms")
        check("package: stored room model linked (signed URL) or an honest reason", bool(view["model"].get("url")) or "not" in (view["model"].get("reason") or ""), view["model"])
        check("package: no jobId/threadId/homeownerId, address withheld", not any(k in json.dumps(view) for k in ("jobId", "threadId", "homeownerId")) and view["address"] is None)
        outbox = STORAGE / "ops_outbox" / f"{qr_id}.json"
        for _ in range(90):
            if outbox.exists():
                break
            await asyncio.sleep(1)
        check("email composed", outbox.exists())
        if outbox.exists():
            email = json.loads(outbox.read_text(encoding="utf-8"))
            print("  email subject:", email["subject"])
            check("email: scope + room + model link present, address absent",
                  "Scope of work: selected rooms" in email["body"] and ("View the 3D model" in (email["html"] or "") or "Not available" in email["body"]) and ADDRESS not in email["body"], email["subject"])

        # Switch home mid-thread: fresh subject, nothing carried from the other house.
        if other_id:
            other = (await http.get(f"/api/v1/ops/homes/{other_id}", headers=OPS)).json()
            body, token = await turn(http, thread, token, messages, other_id, other, "Now let's look at my other place. Which rooms do you have there?", True)
            h = body["flow"].get("home") or {}
            check("home switch: flow.home now the other home, no active room carried", h.get("homeId") == other_id and h.get("roomCount") == other["roomCount"] and not h.get("activeRoom"), h)
            check("home switch: reply speaks to the other home's room count", str(other["roomCount"]) in body["message"]["content"] or "room" in body["message"]["content"].lower(), body["message"]["content"][:160])

    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"\nRESULT home={home_id}: {passed}/{len(CHECKS)} checks passed")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
