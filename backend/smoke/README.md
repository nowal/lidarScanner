# Smoke check results (SOW §4 deliverable)

Committed evidence for the acceptance criteria: "the flow runs end to end …
without critical failures, a quote uploaded by operations comes back to the
user through the agent, smoke checks pass, and logging is verified."

Every run here is **live**: real model (Anthropic claude-sonnet-5, effort
low — the beta configuration of record), real Supabase persistence, real
RoomPlan geometry (the captured-room fixture from `LidarAITests`), driving
the same HTTP endpoints the iOS app calls. Homeowner persona data (name,
address, email) is synthetic; no real personal information appears in these
transcripts.

| File | What it shows |
|---|---|
| `acceptance_rehearsal_<date>.txt` | One full steps 1–10 journey with a PASS/FAIL checklist: grounded opening, scan-wait rule held under an explicit "should I scan more right now?" push, zip/scope/materials/address capture, lead package to ops with address withheld, ops quote upload, the agent presenting and comparing the quotes, and the §12 address release only after selection |
| `reliability_harness_<date>.txt` | The same journey repeated N times with per-check pass RATES (stable / flaky / always-failing) — a single green run hides flakiness; this doesn't |
| `e2e_five_scans_<date>.md` + folder | **The §4 five-scan condition:** the whole-home journey driven over HTTP against five real ingested exports, one file per scan, with a pass/fail checklist each. Covers room naming, an unresolved room the homeowner names, scope in real room names, the lead package with room measurements, the composed email, and a mid-thread home switch |
| `redteam_<date>.txt` (when present) | The adversarial suite: persona hijack, instruction reveal, injection via scan context, scan-gate pressure — see `docs/SECURITY_REDTEAM.md` in the development repo for methodology |

Reproduce any of them from `backend/` (requires the provider key in `.env`;
a run costs cents):

    .venv/Scripts/python scripts/acceptance_rehearsal.py
    .venv/Scripts/python scripts/reliability_harness.py 3

**Pending:** the SOW names *5 sample scans* provided by TakeShape (§8) as
the acceptance substrate. These runs use the one real RoomPlan capture
available to date; the moment TakeShape's sample bundles arrive, the same
rehearsal runs against each and the results land here.
