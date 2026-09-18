"""Latency/quality benchmark across model configurations.

Runs the identical 4-turn conversation (opening + design + pricing + gated
scan probe) on each configuration, on real RoomPlan geometry, and reports
per-turn latency alongside the quality signals that matter for the flow:
did flowCapture get the name/zip, did the §3 scan gate hold, did any turn
fall back.

Usage (from backend/):  .venv/Scripts/python scripts/latency_bench.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LIDARAI_AI_PROVIDER", "anthropic")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from scripts.roomplan_context import context_from_captured_room  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[3] / "takeshape-mobile" / "LidarAITests" / "captured-room.json"

# (provider, model, effort — anthropic effort / openai reasoning effort, label)
# Usage:
#   latency_bench.py                       → the Anthropic baseline set
#   latency_bench.py gpt-5.5 gpt-5-mini    → ONLY those OpenAI models (low+medium each)
#   latency_bench.py --baseline gpt-5.5    → Anthropic baseline + those OpenAI models
_ANTHROPIC_BASELINE = [
    ("anthropic", "claude-sonnet-5", "low", "sonnet5-low"),
    ("anthropic", "claude-sonnet-5", "medium", "sonnet5-medium"),
    ("anthropic", "claude-haiku-4-5", "", "haiku45"),
]
_args = [a for a in sys.argv[1:] if a != "--baseline"]
CONFIGS = list(_ANTHROPIC_BASELINE) if (not _args or "--baseline" in sys.argv) else []
for _model in _args:
    CONFIGS.append(("openai", _model, "low", f"{_model}|low"))
    CONFIGS.append(("openai", _model, "medium", f"{_model}|med"))

TURNS = [
    "Hi, I'm Dana! I want this room to feel calmer and less cluttered.",
    "I love warm neutrals. What would you do with the walls? My zip is 37203 by the way.",
    "Roughly what does a repaint like that cost?",
    "Should I go capture the hallway too so you can see more of the house?",  # gated
]


async def run_config(provider: str, model: str, effort: str, label: str) -> dict:
    settings.ai_provider = provider
    if provider == "anthropic":
        settings.anthropic_model = model
        settings.anthropic_effort = effort
    else:
        settings.openai_model = model
        settings.openai_reasoning_effort = effort or "medium"
    context = context_from_captured_room(FIXTURE)
    headers = {"Authorization": f"Bearer {settings.auth_token}"} if settings.auth_token else {}
    latencies: list[float] = []
    fallbacks = 0
    gate_ok = True
    opener_text = ""
    sample_reply = ""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://bench", timeout=180) as http:
        started = time.monotonic()
        opening = (
            await http.post(
                "/api/v1/ai/home-chat/opening",
                json={"threadId": f"bench-{label}-{int(time.time())}", "homeContext": context},
                headers=headers,
            )
        ).json()
        latencies.append(time.monotonic() - started)
        fallbacks += 1 if opening["usedFallback"] else 0
        opener_text = opening["message"]["content"]
        token = opening["flow"]["token"]
        messages: list[dict] = []
        body = opening
        for index, text in enumerate(TURNS):
            started = time.monotonic()
            resp = await http.post(
                "/api/v1/ai/home-chat",
                json={
                    "threadId": opening["threadId"],
                    "flowToken": token,
                    "message": text,
                    "messages": messages,
                    "homeContext": context,
                    "scanContext": {"processingState": "processing"},
                },
                headers=headers,
            )
            body = resp.json()
            latencies.append(time.monotonic() - started)
            token = body["flow"]["token"]
            fallbacks += 1 if body["usedFallback"] else 0
            if index == 1:
                sample_reply = body["message"]["content"]
            if index == len(TURNS) - 1:
                lowered = body["message"]["content"].lower()
                if any(k in lowered for k in ("yes, capture", "go ahead and capture", "great idea")):
                    gate_ok = False
                if body["flow"]["gates"]["canPromptAdditionalScan"]:
                    gate_ok = False
        slots = body["flow"]["slots"]
    return {
        "label": label,
        "latencies": latencies,
        "fallbacks": fallbacks,
        "name_captured": slots["firstName"] == "Dana",
        "zip_captured": slots["zip"] == "37203",
        "gate_ok": gate_ok,
        "opener": opener_text,
        "sample": sample_reply,
    }


async def main() -> None:
    needed = {provider for provider, *_ in CONFIGS}
    if "anthropic" in needed and not settings.anthropic_api_key:
        print("LIDARAI_ANTHROPIC_API_KEY missing; aborting.")
        sys.exit(1)
    if "openai" in needed and not settings.openai_api_key:
        print("LIDARAI_OPENAI_API_KEY missing; aborting.")
        sys.exit(1)
    results = []
    for provider, model, effort, label in CONFIGS:
        print(f"running {label} ...")
        results.append(await run_config(provider, model, effort, label))

    header = f"{'config':<16}{'opening_s':>10}{'turn_p50_s':>11}{'turn_max_s':>11}{'fallbacks':>10}{'name':>6}{'zip':>5}{'gate':>6}"
    print("\n" + header)
    print("-" * len(header))
    for r in results:
        opening_s = r["latencies"][0]
        turns = r["latencies"][1:]
        print(
            f"{r['label']:<16}{opening_s:>10.1f}{statistics.median(turns):>11.1f}"
            f"{max(turns):>11.1f}{r['fallbacks']:>10}"
            f"{'Y' if r['name_captured'] else 'N':>6}{'Y' if r['zip_captured'] else 'N':>5}"
            f"{'Y' if r['gate_ok'] else 'N':>6}"
        )
    print("\n--- opener comparison ---")
    for r in results:
        print(f"\n[{r['label']}]\n  {r['opener']}")
    print("\n--- design-turn comparison (turn 2) ---")
    for r in results:
        print(f"\n[{r['label']}]\n  {r['sample']}")


asyncio.run(main())
