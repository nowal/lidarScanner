"""Multi-scene conversation suite: run a full agent conversation on every
downloaded ARKitScenes scene (real photos, varied homeowner personas) and
grade what matters:

- room-type recognition against ground-truth furniture labels
- opener cites at least one actually-present object
- AI-tell scan across every reply (bullets, validation openers, negative
  parallelisms, repeated square footage, banned vocabulary)
- SOW §3 scan gate honored on a scan-probe turn
- zero fallbacks

Transcripts are written next to the scene directory for human review — the
automated checks catch regressions; taste still needs eyes.

Usage (from backend/):
    python scripts/scene_suite.py <scenes_root_dir> [--limit N]
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LIDARAI_AI_PROVIDER", "anthropic")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from scripts.arkitscenes_context import context_from_scene, load_annotation_objects  # noqa: E402

ROOM_TYPE_RULES = [
    ({"oven", "stove", "dishwasher", "refrigerator"}, "kitchen"),
    ({"bed"}, "bedroom"),
    ({"toilet", "bathtub", "shower", "sink"}, "bathroom"),
    ({"washer", "dryer"}, "laundry"),
    ({"sofa"}, "living"),
]

OBJECT_SYNONYMS = {
    "cabinet": ["cabinet", "cabinetry", "cupboard"],
    "sofa": ["sofa", "couch", "seating"],
    "chair": ["chair", "seating"],
    "table": ["table"],
    "bed": ["bed"],
    "shelf": ["shelf", "shelves", "shelving"],
    "refrigerator": ["refrigerator", "fridge"],
    "stove": ["stove", "range", "cooktop"],
    "oven": ["oven", "range"],
    "dishwasher": ["dishwasher"],
    "washer": ["washer", "laundry"],
    "dryer": ["dryer", "laundry"],
    "toilet": ["toilet"],
    "bathtub": ["bathtub", "tub"],
    "sink": ["sink"],
    "fireplace": ["fireplace", "mantel"],
    "tv_monitor": ["tv", "television", "screen"],
    "stool": ["stool"],
}

AI_TELLS = [
    ("bullet_list", re.compile(r"(^|\n)\s*[-•*]\s+\S")),
    ("validation_opener", re.compile(
        r"(?i)^(great|excellent|perfect|love (that|it)|good (choice|call|question|instinct)|what a )")),
    ("negative_parallelism", re.compile(r"(?i)\bnot (just|only|merely)\b.{0,60}\bbut\b")),
    ("heres_what_id_do", re.compile(r"(?i)here'?s what i'?d do")),
    ("banned_vocab", re.compile(
        r"(?i)\b(elevate|transform your|seamless|vibrant|nestled|testament|showcase|cozy retreat|tapestry)\b")),
    ("catchphrase", re.compile(
        r"(?i)\b(good bones|nice bones|to work with|solid foundation|anchor(s|ing)?|great call|great instinct)\b")),
]

PERSONAS = [
    ("vague", ["I want this space to feel cozier, it's kind of blah right now.",
               "Hmm, whatever you think would help most I guess.",
               "Should I go capture another room so you can see more?"]),
    ("playroom", ["We're thinking of turning this into a playroom for our kids.",
                  "Safety and easy-clean surfaces matter most. Maybe new floors?",
                  "Should I scan the rest of the house too?"]),
    ("direct", ["I want to repaint in here, walls and trim.",
                "Warm white, low-VOC. What would that roughly cost? Zip is 37203.",
                "Can I add the hallway to the capture as well?"]),
]


def expected_room_type(labels: set[str]) -> str | None:
    for cues, room in ROOM_TYPE_RULES:
        if labels & cues:
            return room
    return None


def cited_objects(text: str, labels: set[str]) -> list[str]:
    lowered = text.lower()
    hits = []
    for label in labels:
        for synonym in OBJECT_SYNONYMS.get(label, [label]):
            if synonym in lowered:
                hits.append(label)
                break
    return hits


def scan_tells(text: str) -> list[str]:
    tells = [name for name, pattern in AI_TELLS if pattern.search(text)]
    # Density tells (first-person demo feedback, Aug 26): stacked questions
    # and overlong replies read as a lecture, not a conversation.
    if text.count("?") >= 2:
        tells.append("double_question")
    if len(text.split()) > 110:
        tells.append("overlong")
    return tells


async def run_scene(scene_dir: Path, persona_index: int, transcript_file) -> dict:
    context = context_from_scene(scene_dir)
    labels = {o["category"] for o in load_annotation_objects(scene_dir)}
    expected = expected_room_type(labels)
    persona_name, turns = PERSONAS[persona_index % len(PERSONAS)]
    headers = {"Authorization": f"Bearer {settings.auth_token}"} if settings.auth_token else {}
    result = {
        "scene": scene_dir.name, "persona": persona_name, "expectedRoom": expected,
        "roomIdentified": None, "openerCites": [], "tells": [], "fallbacks": 0,
        "gateHeld": True, "sqftMentions": 0, "turnSeconds": [],
    }
    print(f"  [{scene_dir.name}] persona={persona_name} expected={expected} labels={sorted(labels)}")
    transcript_file.write(f"\n=== scene {scene_dir.name} ({persona_name}; expected {expected}) ===\n")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://suite", timeout=180) as http:
        started = time.monotonic()
        opening = (await http.post(
            "/api/v1/ai/home-chat/opening",
            json={"threadId": f"suite-{scene_dir.name}-{int(time.time())}", "homeContext": context},
            headers=headers,
        )).json()
        result["turnSeconds"].append(round(time.monotonic() - started, 1))
        opener = opening["message"]["content"]
        transcript_file.write(f"AGENT: {opener}\n")
        result["fallbacks"] += 1 if opening["usedFallback"] else 0
        result["openerCites"] = cited_objects(opener, labels)
        if expected:
            result["roomIdentified"] = expected in opener.lower() or (
                expected == "living" and "living" in opener.lower())
        # The opener's two-part ask (engagement question + first name) is
        # scripted by SOW steps 1-2 — exempt it from the density tell.
        result["tells"].extend(t for t in scan_tells(opener) if t != "double_question")
        sqft_number = re.findall(r"\b(\d{3,4})\s*(?:sq|square)", opener.lower())
        token = opening["flow"]["token"]
        thread_id = opening["threadId"]
        messages: list[dict] = []
        all_text = opener

        for turn_index, text in enumerate(turns):
            transcript_file.write(f"HOMEOWNER: {text}\n")
            started = time.monotonic()
            resp = (await http.post(
                "/api/v1/ai/home-chat",
                json={"threadId": thread_id, "flowToken": token, "message": text,
                      "messages": messages, "homeContext": context,
                      "scanContext": {"processingState": "processing"}},
                headers=headers,
            )).json()
            result["turnSeconds"].append(round(time.monotonic() - started, 1))
            reply = resp["message"]["content"]
            transcript_file.write(f"AGENT: {reply}\n")
            token = resp["flow"]["token"]
            result["fallbacks"] += 1 if resp["usedFallback"] else 0
            result["tells"].extend(scan_tells(reply))
            all_text += "\n" + reply
            if turn_index == len(turns) - 1 and resp["flow"]["gates"]["canPromptAdditionalScan"]:
                result["gateHeld"] = False
            messages.append({"id": resp["message"]["id"], "role": "homeowner",
                             "content": text, "createdAt": resp["message"]["createdAt"]})
            messages.append(resp["message"])

        if sqft_number:
            result["sqftMentions"] = sum(all_text.count(n) for n in set(sqft_number))
    return result


async def main() -> None:
    root = Path(sys.argv[1])
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 99
    scene_dirs = sorted(d for d in root.iterdir() if d.is_dir() and list(d.glob("*_frames")))[:limit]
    if not scene_dirs:
        print(f"no scenes under {root}")
        sys.exit(1)
    print(f"suite: {len(scene_dirs)} scenes, model={settings.anthropic_model}/{settings.anthropic_effort}")
    transcript_path = root / "suite_transcripts.txt"
    results = []
    with transcript_path.open("w", encoding="utf-8") as transcript_file:
        for index, scene_dir in enumerate(scene_dirs):
            try:
                results.append(await run_scene(scene_dir, index, transcript_file))
            except Exception as exc:  # noqa: BLE001
                print(f"  [{scene_dir.name}] ERROR: {exc}")

    header = f"{'scene':<11}{'persona':<10}{'room':<10}{'roomOK':>7}{'cites':>6}{'tells':>6}{'fallb':>6}{'gate':>5}{'sqft>1':>7}{'p50s':>6}"
    print("\n" + header)
    print("-" * len(header))
    for r in results:
        durations = sorted(r["turnSeconds"])
        p50 = durations[len(durations) // 2] if durations else 0
        room_ok = {True: "Y", False: "N", None: "-"}[r["roomIdentified"]]
        print(f"{r['scene']:<11}{r['persona']:<10}{str(r['expectedRoom']):<10}{room_ok:>7}"
              f"{len(r['openerCites']):>6}{len(r['tells']):>6}{r['fallbacks']:>6}"
              f"{'Y' if r['gateHeld'] else 'N':>5}{'Y' if r['sqftMentions'] > 1 else 'n':>7}{p50:>6}")
    tell_counts: dict[str, int] = {}
    for r in results:
        for tell in r["tells"]:
            tell_counts[tell] = tell_counts.get(tell, 0) + 1
    print(f"\ntells breakdown: {tell_counts or 'none'}")
    print(f"transcripts: {transcript_path}")


asyncio.run(main())
