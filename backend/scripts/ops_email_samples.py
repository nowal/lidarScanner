"""Compose sample operations lead emails from the fictional provider fixture.

Three scenarios, each written as plain text and HTML to ``docs/samples/``:

* ``urban``        Painting in 37203 -- seven candidates, every relationship
                   type, numbers on most rows (well-covered market)
* ``rural``        Power Washing in 37033 -- two candidates, one with seven
                   reviews, one with nothing on file (sparse market)
* ``conflicting``  Window Cleaning in 37203 -- a four-time past quoter with
                   a 3.4 rating, a stranger with 4.9 on 388 reviews, a
                   social-only company with no reviews anywhere, and a
                   partner with nothing on file

Everything is fictional: rows come from ``tests/fixtures/provider_candidates.json``
(not the demo seed, not the persona transcripts), the homeowner is a stand-in
with a 555 number and an example.com address, the zip is the only location,
and the model link points at a placeholder host. No real address, phone
number, or client home identifier appears anywhere.

Run::

    .venv/Scripts/python scripts/ops_email_samples.py            # writes docs/samples/
    .venv/Scripts/python scripts/ops_email_samples.py --golden   # refreshes tests/fixtures/golden/

The same composition feeds the golden-file tests (tests/test_ops_email_rendering.py),
so a rendering change shows up as a diff in both places.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.flow import partners  # noqa: E402
from app.flow.ops_email import build_ops_email, build_ops_email_html  # noqa: E402
from app.flow_quotes import QuoteRequestRecord  # noqa: E402

FIXTURE = BACKEND / "tests" / "fixtures" / "provider_candidates.json"
SAMPLES_DIR = BACKEND / "docs" / "samples"
GOLDEN_DIR = BACKEND / "tests" / "fixtures" / "golden"

SCENARIOS: dict[str, dict] = {
    "urban": dict(
        id="qr_sample_urban", serviceType="Painting", zip="37203", roomName="kitchen",
        scopeIntent="single_room", scopeRooms=["kitchen"],
        scopeOptions=["walls + trim", "ceiling"], materials=["low-VOC eggshell"],
        synopsis=(
            "Dana wants the kitchen repainted in a warm off-white with the trim in "
            "semi-gloss, ideally before the holidays. Asked whether the ceiling "
            "should be done at the same time."
        ),
        measurements={"floorAreaSquareFeet": 214, "wallCount": 4, "windowCount": 2,
                      "rooms": [{"name": "kitchen", "floorAreaSquareFeet": 214}]},
        researched=[{"name": "Found Online Painting LLC", "phone": "(615) 555-0199",
                     "website": "https://foundonlinepainting.example",
                     "note": "residential repaints; found in web search, unvetted"}],
    ),
    "rural": dict(
        id="qr_sample_rural", serviceType="Power Washing", zip="37033", roomName=None,
        scopeIntent="whole_home", scopeRooms=[],
        scopeOptions=["siding", "front porch", "driveway"], materials=[],
        synopsis=(
            "Sam has a two-storey farmhouse with vinyl siding that has not been "
            "washed in years, plus a concrete driveway and a covered porch. "
            "Flexible on timing; wants one visit for all three."
        ),
        measurements={},
        researched=None,
    ),
    "conflicting": dict(
        id="qr_sample_conflict", serviceType="Window Cleaning", zip="37203", roomName="living room",
        scopeIntent="selected_rooms", scopeRooms=["living room", "primary bedroom"],
        scopeOptions=["interior + exterior", "screens"], materials=[],
        synopsis=(
            "Alex wants every window in a 1920s bungalow cleaned inside and out "
            "before listing photos next month, screens included. Mentioned two "
            "second-floor windows that are hard to reach."
        ),
        measurements={"windowCount": 14},
        researched=None,
    ),
}

HOMEOWNERS = {
    "urban": dict(firstName="Dana", contactEmail="dana@example.com", contactPhone="(615) 555-0100"),
    "rural": dict(firstName="Sam", contactEmail="sam@example.com", contactPhone="(931) 555-0100"),
    "conflicting": dict(firstName="Alex", contactEmail="alex@example.com", contactPhone="(615) 555-0101"),
}


def install_fixture_table(storage_dir: Path) -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "partners.json").write_text(json.dumps(rows), encoding="utf-8")


def sample_record(name: str) -> QuoteRequestRecord:
    s = SCENARIOS[name]
    return QuoteRequestRecord(
        id=s["id"], createdAt="2026-09-09T14:30:00+00:00", threadId=f"thread-{name}-not-in-email",
        homeownerId="00000000-0000-0000-0000-000000000000", status="submitted",
        serviceType=s["serviceType"], roomName=s["roomName"], zip=s["zip"],
        scopeIntent=s["scopeIntent"], scopeRooms=s["scopeRooms"],
        address="1 Withheld Street, Not In The Email",
        scopeOptions=s["scopeOptions"], materials=s["materials"], synopsis=s["synopsis"],
        measurements=s["measurements"],
        modelLink={"kind": "supabase_signed_url", "jobId": "job-not-in-email",
                   "url": "https://storage.example.com/home-assets/flow-models/sample.usdz?token=sample",
                   "note": "Signed link, valid ~30 days; no credentials needed."},
        **HOMEOWNERS[name],
    )


def compose(name: str, storage_dir: Path) -> tuple[str, str, str]:
    """(subject, plain text, html) for one scenario, composed exactly the way
    send_ops_email composes a real lead (minus transport)."""
    install_fixture_table(storage_dir)
    settings.storage_dir = str(storage_dir)
    record = sample_record(name)
    researched = SCENARIOS[name]["researched"]
    partner_rows = partners.find_partners(record.serviceType, record.zip)
    prospects = partners.find_prospects(record.serviceType, record.zip)
    ranked = partners.rank_for_lead(record.serviceType, record.zip, researched)
    subject, text = build_ops_email(record, partner_rows, researched, prospects, ranked)
    html = build_ops_email_html(record, partner_rows, researched, prospects, ranked)
    return subject, text, html


def write_samples(out_dir: Path = SAMPLES_DIR) -> list[Path]:
    settings.public_base_url = "https://ops.example.com"
    settings.preferred_partner_ordering_enabled = False
    settings.ops_reply_enabled = True
    written: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name in SCENARIOS:
            subject, text, html = compose(name, Path(tmp) / name)
            txt_path = out_dir / f"ops-email-{name}.txt"
            txt_path.write_text(f"Subject: {subject}\n\n{text}\n", encoding="utf-8")
            html_path = out_dir / f"ops-email-{name}.html"
            html_path.write_text(html, encoding="utf-8")
            written += [txt_path, html_path]
    return written


def write_golden(out_dir: Path = GOLDEN_DIR) -> list[Path]:
    """Deterministic variant: no public base URL (the entry link carries a
    timestamped signature), no reply note."""
    settings.public_base_url = ""
    settings.preferred_partner_ordering_enabled = False
    settings.ops_reply_enabled = False
    written: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for name in SCENARIOS:
            subject, text, html = compose(name, Path(tmp) / name)
            txt_path = out_dir / f"ops_email_{name}.txt"
            txt_path.write_text(f"Subject: {subject}\n\n{text}\n", encoding="utf-8")
            html_path = out_dir / f"ops_email_{name}.html"
            html_path.write_text(html, encoding="utf-8")
            written += [txt_path, html_path]
    return written


if __name__ == "__main__":
    paths = write_golden() if "--golden" in sys.argv else write_samples()
    for path in paths:
        print(path.relative_to(BACKEND))
