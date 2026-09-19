"""Live smoke run of the flow agent against the real Anthropic API.

Usage (from backend/, with LIDARAI_ANTHROPIC_API_KEY in .env or the env):

    .venv/Scripts/python scripts/anthropic_smoke.py

Runs the opening turn plus a short scripted conversation in-process (no
server needed) and prints the transcript, flow state, and gate decisions.
Spends a few cents of real API usage per run.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LIDARAI_AI_PROVIDER", "anthropic")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from scripts.roomplan_context import context_from_captured_room  # noqa: E402

# Real RoomPlan capture from TakeShape's iOS test fixtures (living space:
# 8.8m x 6.0m, 10 walls, 2 windows, sofas/chairs/tables/storage).
_FIXTURE = Path(__file__).resolve().parents[3] / "takeshape-mobile" / "LidarAITests" / "captured-room.json"

CONTEXT = context_from_captured_room(_FIXTURE)

TURNS = [
    "Hi! I'm Dana. I'd love this room to feel calmer, it's way too cluttered.",
    "I like soft, warm colors. What would you do with the walls?",
    "How much would repainting something like this roughly cost?",
    "Should I capture the hallway too so you can see more?",  # gated: processing
]


async def main() -> None:
    if not settings.anthropic_api_key:
        print("LIDARAI_ANTHROPIC_API_KEY is not set (backend/.env or env var). Aborting.")
        sys.exit(1)
    print(f"provider={settings.ai_provider} model={settings.anthropic_model} effort={settings.anthropic_effort}\n")
    headers = {"Authorization": f"Bearer {settings.auth_token}"} if settings.auth_token else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://smoke", timeout=120) as http:
        opening = await http.post(
            "/api/v1/ai/home-chat/opening",
            json={"homeContext": CONTEXT},
            headers=headers,
        )
        opening.raise_for_status()
        body = opening.json()
        thread_id, token = body["threadId"], body["flow"]["token"]
        print(f"AGENT (opening, {body['model']}):\n  {body['message']['content']}\n")

        messages = []
        for turn in TURNS:
            messages_payload = list(messages)
            print(f"HOMEOWNER:\n  {turn}\n")
            resp = await http.post(
                "/api/v1/ai/home-chat",
                json={
                    "threadId": thread_id,
                    "flowToken": token,
                    "message": turn,
                    "messages": messages_payload,
                    "homeContext": CONTEXT,
                    "scanContext": {"processingState": "processing"},
                },
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()
            token = body["flow"]["token"]
            flow = body["flow"]
            print(f"AGENT ({body['model']}, fallback={body['usedFallback']}):\n  {body['message']['content']}\n")
            print(
                "  flow: step={step} slots={slots} gates={gates}".format(
                    step=flow["stepName"],
                    slots=json.dumps(flow["slots"], ensure_ascii=False),
                    gates=json.dumps(flow["gates"]),
                )
            )
            if body.get("priceGuidance"):
                print(f"  priceGuidance: {json.dumps(body['priceGuidance'])}")
            print()
            messages.append({"id": body["message"]["id"], "role": "homeowner", "content": turn,
                             "createdAt": body["message"]["createdAt"]})
            messages.append(body["message"])

    print("Journal:", Path(settings.storage_dir) / "flow_journal" / "journal.jsonl")


if __name__ == "__main__":
    asyncio.run(main())
