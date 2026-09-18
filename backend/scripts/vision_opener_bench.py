"""Image-grounded opener benchmark: run the opening turn with REAL room
photos (an ARKitScenes scene) across model configs, and print each opener
next to the scene's ground-truth object labels so hallucinations are
checkable at a glance.

Usage (from backend/):
    python scripts/vision_opener_bench.py <scene_dir> [openai-model ...]
Runs the Anthropic baseline (sonnet5 low + medium opener) always; any CLI
model ids are added as OpenAI configs.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LIDARAI_AI_PROVIDER", "anthropic")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from scripts.arkitscenes_context import context_from_scene, load_annotation_objects  # noqa: E402


async def run_opener(provider: str, model: str, effort: str, context: dict) -> tuple[str, float, bool]:
    settings.ai_provider = provider
    if provider == "anthropic":
        settings.anthropic_model = model
        settings.anthropic_effort = effort
    else:
        settings.openai_model = model
        settings.openai_reasoning_effort = effort or "medium"
    headers = {"Authorization": f"Bearer {settings.auth_token}"} if settings.auth_token else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://vb", timeout=180) as http:
        started = time.monotonic()
        resp = await http.post(
            "/api/v1/ai/home-chat/opening",
            json={
                "threadId": f"vision-{provider}-{model}-{effort}-{int(time.time())}",
                "homeContext": context,
            },
            headers=headers,
        )
        body = resp.json()
        return body["message"]["content"], time.monotonic() - started, body["usedFallback"]


async def main() -> None:
    scene_dir = Path(sys.argv[1])
    openai_models = sys.argv[2:]
    context = context_from_scene(scene_dir)
    labels = sorted({o["category"] for o in load_annotation_objects(scene_dir)})
    print(f"GROUND TRUTH objects: {', '.join(labels)}")
    print(f"context: {context['floorplanSummary']} keyframes={len(context['selectedKeyframes'])}\n")

    configs = [
        ("anthropic", "claude-sonnet-5", "low", "sonnet5|low"),
        ("anthropic", "claude-sonnet-5", "medium", "sonnet5|med"),
    ]
    for m in openai_models:
        configs.append(("openai", m, "low", f"{m}|low"))

    for provider, model, effort, label in configs:
        text, seconds, fallback = await run_opener(provider, model, effort, context)
        print(f"[{label}] {seconds:.1f}s fallback={fallback}\n  {text}\n")


asyncio.run(main())
