"""Does the agent follow context clues to the RIGHT service, or default to
paint/floor? Each case gives a clue that clearly implies one non-paint/floor
service; we check which service family the reply steers toward."""
import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LIDARAI_AI_PROVIDER", "anthropic")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from scripts.roomplan_context import context_from_captured_room  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "demo_assets" / "captured-room.json"

FAMILIES = {
    "cleaning": re.compile(r"(?i)deep (interior )?clean|cleaning|cleaner|move-in clean|scrub|declutter"),
    "window_cleaning": re.compile(r"(?i)window|streaky|interior and exterior|both sides|glass"),
    "power_washing": re.compile(r"(?i)power wash|pressure wash"),
    "decking": re.compile(r"(?i)deck|railing|porch"),
    "painting": re.compile(r"(?i)\bpaint|repaint|wall color"),
    "flooring": re.compile(r"(?i)\bfloor|hardwood|refinish|carpet|vinyl|\btile"),
}

# (clue, the service family we'd want it to steer toward)
CASES = [
    ("Honestly the place is just filthy — we're moving in next week and it needs a serious top-to-bottom scrubbing before we bring furniture in.", "cleaning"),
    ("I can barely see out the windows, they're covered in grime and streaks inside and out.", "window_cleaning"),
    ("The patio and driveway are covered in green algae and years of dirt, looks awful.", "power_washing"),
    ("Our back deck boards are grey and splitting, the railing wobbles — it's seen better days.", "decking"),
    ("Just did a big renovation and there's construction dust absolutely everywhere, every surface.", "cleaning"),
    ("The siding on the house has gotten really dingy and streaked over the years.", "power_washing"),
]


async def main():
    context = context_from_captured_room(FIXTURE)
    headers = {"Authorization": f"Bearer {settings.auth_token}"} if settings.auth_token else {}
    correct = 0
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://sr", timeout=180) as http:
        for i, (clue, want) in enumerate(CASES):
            # Skip the opener; go straight to the clue as the first real turn.
            opening = (await http.post("/api/v1/ai/home-chat/opening",
                       json={"threadId": f"sr-{i}", "homeContext": context}, headers=headers)).json()
            token = opening["flow"]["token"]
            resp = (await http.post("/api/v1/ai/home-chat",
                json={"threadId": f"sr-{i}", "flowToken": token, "message": clue,
                      "messages": [], "homeContext": context,
                      "scanContext": {"processingState": "complete"}}, headers=headers)).json()
            reply = resp["message"]["content"]
            matched = [fam for fam, rx in FAMILIES.items() if rx.search(reply)]
            hit = want in matched
            defaulted = ("painting" in matched or "flooring" in matched) and not hit
            correct += hit
            flag = "RIGHT" if hit else ("PAINT/FLOOR DEFAULT" if defaulted else "other")
            print(f"[{flag:^18}] want={want}\n    matched={matched}\n    {reply[:160]}\n")
    print("=" * 60)
    print(f"{correct}/{len(CASES)} steered to the contextually-right service")


asyncio.run(main())
