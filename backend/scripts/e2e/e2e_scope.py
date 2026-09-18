"""Scope-intent end-to-end check against the running backend (port 8010),
reusing the launcher and the conventions of e2e_ops_email.py.

    python e2e_scope.py single_room|selected_rooms|whole_home [processor_job|device_bake]

Per run: steps 1-10 with a scope statement in the design conversation, a
hard-constraint probe while processing (must produce no scan suggestion),
a step-6 probe once the flag is set (extension behaviour must match the
scope), scope in the ops view and the captured email, then ops upload and
the results turn. The signal argument must match the server's
LIDARAI_SCAN_COMPLETE_SIGNAL (set via E2E_SIGNAL in run_e2e_server.py).
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
from scripts.roomplan_context import context_from_captured_room  # noqa: E402

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8010")
STORAGE = Path(os.environ.get("E2E_STORAGE_DIR", str(BACKEND / "backend_storage")))
FIXTURE = BACKEND.parents[1] / "takeshape-mobile" / "LidarAITests" / "captured-room.json"
OPS = {"Authorization": "Bearer " + os.environ.get("E2E_OPS_TOKEN", "e2e-ops-token")}
ADDRESS = "118 Maple Street"

SCOPE_STATEMENT = {
    "single_room": "Just this room, honestly — the living room is the whole project.",
    "selected_rooms": "A couple of rooms: this one and the kitchen. Those two are the project, nothing else.",
    "whole_home": "Honestly the whole house needs a refresh, every room.",
}
HARD_PROBE = "Should I go scan the rest of the house right now so you can see everything?"
STEP6_PROBE = "My model is ready now. Should I add more of the house to it?"

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def scan_context(state, signal, ready):
    ctx = {"processingState": state}
    if signal == "device_bake":
        ctx["localModelReady"] = ready
    return ctx


async def turn(http, thread, token, messages, context, text, scan_state, signal, ready):
    context["meshSummary"]["photorealStatus"] = "ready" if scan_state == "complete" else "processing"
    resp = await http.post("/api/v1/ai/home-chat", json={
        "threadId": thread, "flowToken": token, "message": text, "messages": messages,
        "homeContext": context, "scanContext": scan_context(scan_state, signal, ready)})
    body = resp.json()
    print(f"HOMEOWNER: {text}\nAGENT: {body['message']['content'][:220]}...\n"
          f"   gates={body['flow']['gates']} scope={body['flow']['slots']['scopeIntent']} "
          f"rooms={body['flow']['slots']['scopeRooms']} wording={body['flow']['wordingId']}")
    messages.append({"id": body["message"]["id"], "role": "homeowner", "content": text,
                     "createdAt": body["message"]["createdAt"]})
    messages.append(body["message"])
    return body, body["flow"]["token"]


def last_journal(thread):
    path = STORAGE / "flow_journal" / f"{re.sub(r'[^A-Za-z0-9_-]', '_', thread)}.jsonl"
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


async def main(scope, signal):
    context = context_from_captured_room(FIXTURE)
    thread = f"e2e-scope-{scope}-{signal}-{int(time.time())}"
    async with httpx.AsyncClient(base_url=BASE, timeout=180) as http:
        health = (await http.get("/health")).json()
        print("health:", health["status"])
        opening = (await http.post("/api/v1/ai/home-chat/opening",
                                   json={"homeContext": context, "threadId": thread})).json()
        token = opening["flow"]["token"]
        messages = []
        print(f"AGENT (opening): {opening['message']['content'][:160]}...")

        # Steps 2-3: name, design talk, the scope statement, while processing.
        body, token = await turn(http, thread, token, messages, context,
                                 "Hi! I'm Dana. This room feels dated and I'd like to freshen it up.",
                                 "processing", signal, False)
        body, token = await turn(http, thread, token, messages, context,
                                 "Warm neutrals sound lovely. " + SCOPE_STATEMENT[scope],
                                 "processing", signal, False)
        check(f"step 3: scope captured as {scope}", body["flow"]["slots"]["scopeIntent"] == scope,
              body["flow"]["slots"]["scopeIntent"])
        if scope == "selected_rooms":
            rooms = [r.lower() for r in body["flow"]["slots"]["scopeRooms"]]
            check("step 3: two named rooms recorded", len(rooms) >= 2 and any("kitchen" in r for r in rooms), rooms)

        # Hard constraint probe while processing (both signals: flag not set).
        body, token = await turn(http, thread, token, messages, context, HARD_PROBE, "processing", signal, False)
        gates = body["flow"]["gates"]
        reply = body["message"]["content"]
        check("HARD: gate closed while processing", gates["canPromptAdditionalScan"] is False and gates["extensionPromptMode"] == "closed")
        check("HARD: reply contains no scan suggestion", not enforcement._SCAN_SUGGESTION.search(reply), reply[:120])
        j = last_journal(thread)
        check(f"HARD: journal records signal={signal}", j["flow"]["scanSignal"] == signal)

        body, token = await turn(http, thread, token, messages, context, "Sure — my zip code is 37203.", "processing", signal, False)
        check("step 4: zip captured", body["flow"]["slots"]["zip"] == "37203")

        # Flag set: the step-6 probe.
        body, token = await turn(http, thread, token, messages, context, STEP6_PROBE, "complete", signal, True)
        gates = body["flow"]["gates"]
        reply = body["message"]["content"]
        check("step 6: gate open once the flag is set", gates["canPromptAdditionalScan"] is True, gates)
        expected_mode = {"single_room": ("generic_once", "none"), "selected_rooms": ("named_rooms",), "whole_home": ("open",)}[scope]
        check(f"step 6: extension mode for {scope}", gates["extensionPromptMode"] in expected_mode, gates["extensionPromptMode"])
        broad = enforcement._BROAD_EXTENSION.search(reply)
        if scope in ("single_room", "selected_rooms"):
            check("step 6: no rest-of-the-home / other-rooms invitation", not broad, reply[:160])
            if scope == "selected_rooms" and enforcement._SCAN_SUGGESTION.search(reply):
                check("step 6: any invitation names a chosen room", "kitchen" in reply.lower() or "living" in reply.lower(), reply[:160])
        else:
            check("step 6: whole-home invitation permitted (reply not stripped to safe copy)",
                  "still being prepared" not in reply, reply[:160])
        j = last_journal(thread)
        # A suppressed draft is the enforcement doing its job (one regeneration);
        # what must never happen is the DELIVERED reply carrying one, or the
        # turn collapsing to the wait copy while the model is ready.
        if j["suppressedDrafts"]:
            print("  (enforcement regenerated:", [d["violations"] for d in j["suppressedDrafts"]], ")")
        check("step 6: delivered reply is not the processing-wait safe copy", "still being prepared" not in reply, reply[:120])
        check("step 6: hard-rule violations never appear once the flag is set",
              not any(v == "scan_suggestion_while_processing" for d in j["suppressedDrafts"] for v in d["violations"]))

        # Under single_room the one generic offer, if made, must not repeat.
        if scope == "single_room" and gates["extensionPromptMode"] == "none":
            body, token = await turn(http, thread, token, messages, context, "No, just this room. What about the trim?", "complete", signal, True)
            check("step 6: second turn makes no further invitation",
                  not enforcement._SCAN_SUGGESTION.search(body["message"]["content"]) and not enforcement._GENERIC_OFFER.search(body["message"]["content"]),
                  body["message"]["content"][:160])

        # Steps 7-9.
        body, token = await turn(http, thread, token, messages, context,
                                 "Let's do the repaint. Walls and trim, in a low-VOC warm white.", "complete", signal, True)
        body, token = await turn(http, thread, token, messages, context,
                                 f"Yes, let's get quotes. My address is {ADDRESS}, Nashville TN, and I'm at dana@example.com.",
                                 "complete", signal, True)
        submitted = await http.post("/api/v1/ai/quote-requests",
                                    json={"threadId": thread, "flowToken": token, "confirm": True, "homeContext": context})
        check("step 9: quote request accepted", submitted.status_code == 201, submitted.text[:160])
        qr_id = submitted.json().get("quoteRequestId")
        token = submitted.json().get("flowToken", token)
        if not qr_id:
            return
        view = (await http.get(f"/api/v1/ops/quote-requests/{qr_id}", headers=OPS)).json()
        check(f"ops view: scope intent {scope}", view["project"]["scope"]["intent"] == scope, view["project"]["scope"])
        check("ops view: address withheld, no thread id", view["address"] is None and thread not in json.dumps(view))

        outbox = STORAGE / "ops_outbox" / f"{qr_id}.json"
        for _ in range(90):
            if outbox.exists():
                break
            await asyncio.sleep(1)
        check("email composed", outbox.exists())
        if outbox.exists():
            email = json.loads(outbox.read_text(encoding="utf-8"))
            line = next((l.strip() for l in email["body"].splitlines() if "Scope of work" in l), "")
            print("  email:", email["subject"], "|", line)
            expect = {"single_room": "one room", "selected_rooms": "selected rooms:", "whole_home": "whole home"}[scope]
            check(f"email: scope line says {expect}", expect in line, line)
            subject_ok = {"single_room": True,
                          "selected_rooms": " + " in email["subject"] and "kitchen" in email["subject"].lower(),
                          "whole_home": "whole home" in email["subject"]}[scope]
            check("email: subject reflects scope", subject_ok, email["subject"])
            check("email: address withheld", ADDRESS not in email["body"] and ADDRESS not in (email["html"] or ""))

        up = await http.post(f"/api/v1/ops/quote-requests/{qr_id}/quotes", headers=OPS, json={"quotes": [
            {"providerName": "Brightline Painting", "priceUsd": 2450}]})
        check("step 10: ops upload accepted", up.status_code == 200)
        body, token = await turn(http, thread, token, messages, context, "Any news on my quotes?", "complete", signal, True)
        check("step 10: quote presented", "brightline" in body["message"]["content"].lower()
              and body["flow"]["quoteRequest"]["status"] == "presented")

    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"\nRESULT scope={scope} signal={signal}: {passed}/{len(CHECKS)} checks passed")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "processor_job"))
