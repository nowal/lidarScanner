"""Drive a real lead through the running backend (http://127.0.0.1:8010) and
check the recent work end to end from our side: live model turns, live dev
Supabase, the ops email composed by the startup-owned worker (captured in
the outbox because every transport is blanked), quote upload promoting the
providers into the durable table, and -- in phase 2, after a restart with a
wiped disk -- the next lead's email marking them as past quoters.

    python e2e_ops_email.py 1     # first lead, upload quotes, results turn
    python e2e_ops_email.py 2     # after restart + wiped storage: second lead
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
from scripts.roomplan_context import context_from_captured_room  # noqa: E402

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8010")
STORAGE = Path(os.environ.get("E2E_STORAGE_DIR", str(BACKEND / "backend_storage")))
FIXTURE = BACKEND.parents[1] / "takeshape-mobile" / "LidarAITests" / "captured-room.json"
OPS = {"Authorization": "Bearer " + os.environ.get("E2E_OPS_TOKEN", "e2e-ops-token")}
ADDRESS = "118 Maple Street"

TURNS = [
    ("Hi! I'm Dana. This room feels dated and I'd like to freshen it up.", "processing"),
    ("Warm neutrals sound lovely. Could you also check — is my 3D model done yet?", "processing"),
    ("Sure — my zip code is 37203.", "processing"),
    ("Great, it says my model is ready now!", "complete"),
    ("Let's do the repaint. I'd want walls and trim done, in a low-VOC warm white.", "complete"),
    (f"Yes, let's get quotes. My address is {ADDRESS}, Nashville TN, and I'm at dana@example.com.", "complete"),
]

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def env():
    out = {}
    for line in (BACKEND / ".env").read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def supabase_partner_rows():
    e = env()
    url, key = e["LIDARAI_SUPABASE_URL"].rstrip("/"), e["LIDARAI_SUPABASE_SERVICE_ROLE_KEY"]
    r = httpx.get(f"{url}/rest/v1/flow_partners", params={"select": "key,relationship,record"},
                  headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=20)
    r.raise_for_status()
    return {row["key"]: row for row in r.json()}


async def run_lead(http, phase):
    context = context_from_captured_room(FIXTURE)
    thread = f"e2e-phase{phase}-{int(time.time())}"
    opening = (await http.post("/api/v1/ai/home-chat/opening",
                               json={"homeContext": context, "threadId": thread})).json()
    token = opening["flow"]["token"]
    print(f"\nAGENT (opening): {opening['message']['content'][:160]}...")
    messages = []
    body = opening
    for text, scan_state in TURNS:
        context["meshSummary"]["photorealStatus"] = "ready" if scan_state == "complete" else "processing"
        resp = await http.post("/api/v1/ai/home-chat", json={
            "threadId": thread, "flowToken": token, "message": text, "messages": messages,
            "homeContext": context, "scanContext": {"processingState": scan_state}})
        body = resp.json()
        token = body["flow"]["token"]
        print(f"HOMEOWNER: {text}\nAGENT ({body['model']}, fallback={body['usedFallback']}): "
              f"{body['message']['content'][:200]}...\n   slots={body['flow']['slots']}")
        messages.append({"id": body["message"]["id"], "role": "homeowner", "content": text,
                         "createdAt": body["message"]["createdAt"]})
        messages.append(body["message"])
    submitted = await http.post("/api/v1/ai/quote-requests",
                                json={"threadId": thread, "flowToken": token, "confirm": True,
                                      "homeContext": context})
    check("step 9: quote request accepted", submitted.status_code == 201, submitted.text[:200])
    qr_id = submitted.json().get("quoteRequestId")
    token = submitted.json().get("flowToken", token)
    return thread, token, messages, context, qr_id


async def wait_outbox(qr_id, timeout=90):
    path = STORAGE / "ops_outbox" / f"{qr_id}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        await asyncio.sleep(1)
    return None


def provider_section(body):
    return body.split("SUGGESTED PROVIDERS")[1].split("ENTER THE CHECKED QUOTE")[0]


async def main(phase):
    async with httpx.AsyncClient(base_url=BASE, timeout=180) as http:
        health = (await http.get("/health")).json()
        print("health:", health["status"], health.get("configWarnings"))
        thread, token, messages, context, qr_id = await run_lead(http, phase)
        if not qr_id:
            return

        view = (await http.get(f"/api/v1/ops/quote-requests/{qr_id}", headers=OPS)).json()
        flat = json.dumps(view)
        check("ops view: no threadId / homeownerId / jobId", not any(k in flat for k in ("threadId", "homeownerId", "jobId")))
        check("ops view: address withheld before selection", view["address"] is None)

        email = await wait_outbox(qr_id)
        check("lead email composed by the worker (outbox capture)", email is not None)
        if email:
            body, html = email["body"], email["html"] or ""
            section = provider_section(body)
            print("\n--- provider section of the lead email ---" + section + "---")
            check("email: ranked section present", "Ranked by review and social presence" in body)
            check("email: address never included", ADDRESS not in body and ADDRESS not in html)
            check("email: no thread id in either part", thread not in body and thread not in html)
            check("email: entry link present", "/ops/entry/" in body)
            if phase == 2:
                check("phase 2: Brightline marked PAST QUOTER x1", "[PAST QUOTER x1] Brightline Painting" in section)
                check("phase 2: Harbor Coatings listed as quoted", "Harbor Coatings | quoted 1x through TakeShape" in section)
                check("phase 2: no sample seed rows", "SAMPLE ROW" not in section)
                check("phase 2: HTML carries the PAST QUOTER badge", "PAST QUOTER" in html and "&times;1" in html)
            link = next((l.strip() for l in body.splitlines() if "/ops/entry/" in l), None)
            if link:
                page = await http.get(link.replace(BASE, ""))
                check("entry page opens from the emailed link", page.status_code == 200 and ADDRESS not in page.text)

        if phase == 1:
            up = await http.post(f"/api/v1/ops/quote-requests/{qr_id}/quotes", headers=OPS, json={"quotes": [
                {"providerName": "Brightline Painting", "priceUsd": 2450, "notes": "Can start week of Sep 21"},
                {"providerName": "Harbor Coatings", "priceLowUsd": 2100, "priceHighUsd": 2900},
                {"providerName": "Illustrative Only", "priceUsd": 1, "isEstimate": True},
            ]})
            check("step 10: ops upload accepted", up.status_code == 200, up.text[:120])
            await asyncio.sleep(3)  # fire-and-forget durable write
            rows = supabase_partner_rows()
            check("durable: Brightline in flow_partners as quoted x1",
                  rows.get("brightline painting", {}).get("record", {}).get("quotedCount") == 1
                  and rows["brightline painting"]["relationship"] == "quoted")
            check("durable: Harbor in flow_partners", "harbor coatings" in rows)
            check("durable: illustrative estimate NOT promoted", "illustrative only" not in rows)
            local = json.loads((STORAGE / "partners.json").read_text(encoding="utf-8"))
            check("local cache written too", any(r["name"] == "Brightline Painting" for r in local))
            resp = await http.post("/api/v1/ai/home-chat", json={
                "threadId": thread, "flowToken": token, "message": "Any news on my quotes?",
                "messages": messages, "homeContext": context, "scanContext": {"processingState": "complete"}})
            body = resp.json()
            print(f"\nAGENT (results): {body['message']['content'][:300]}...")
            low = body["message"]["content"].lower()
            check("step 10: agent presents both quotes", "brightline" in low and "harbor" in low)
            check("step 10: request marked presented",
                  body["flow"]["quoteRequest"]["status"] == "presented")
        else:
            rows = supabase_partner_rows()
            check("phase 2: durable table still holds Brightline after restart",
                  rows.get("brightline painting", {}).get("record", {}).get("quotedCount") == 1)

    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"\nRESULT phase {phase}: {passed}/{len(CHECKS)} checks passed")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1))
