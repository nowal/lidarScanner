# Quintin intake integration — October 27, 2026

## Architecture decision

Keep the TakeShape backend in `nowal/lidarScanner`, under `backend/`. Keep the
native app in `nowal/takeshape-mobile`. Moving repositories during this release
would add deployment and credential changes without improving isolation: the
intake already has its own module, routes, prompt, rubrics, state and Anthropic
adapter, and needs the existing scan authorization, immutable model snapshots,
provider gallery, quote receipts and operations delivery.

The production-facing dashboard uses `HomeQuoteRequestPanel` and
`QuoteIntakeViewModel`, calling `/api/v1/ai/quote-intake`. The retained
`HomeAIChatViewModel(conversationEnabled: false)` supplies quote/approval helpers;
it must not enable the legacy conversation state machine.

Preserve Nathan and Chance's agent in place. Its `home_ai.py`, `flow_runtime.py`,
`home_guide_prompt.py`, `flow/machine.py`, `flow/service_rubrics.py`,
`home_guide_tools.py`, `docs/SERVICE_RUBRICS.md`, existing routes and saved
conversations remain available. The integration restores the shared legacy
catalog and rubric document to their pre-PR contents. Landscaping is an
intake-only catalog extension. The global AI provider setting is unchanged.
The new intake never silently falls back to the old agent.

## Source and compatibility

Starting production branches:

- Backend `407e85b9388b86bdbc23a0e4460be47d306c4b3f`.
- Mobile `1f67d84a4f9073ed51a5309c8a5621e92f8bc8d3`.

Quintin's reviewed branches:

- [Backend PR #15](https://github.com/nowal/lidarScanner/pull/15),
  `c3fd045b633ecd5402dea96e26801ae5367b99df`.
- [Mobile PR #11](https://github.com/nowal/takeshape-mobile/pull/11),
  `fde8ae00d11ee7bf6d6e308b0ba03d373ad3d1ad`.

Integration work uses `codex/quintin-intake-integration` in each active checkout.
The obsolete `lidarTakeShape/LidarAI` checkout is untouched.

No new database migration is required. The existing `quote_intakes.state` and
quote-request JSON records hold the additive fields. Previously pinned v1
rubrics remain readable without changing stored questions, hashes or prompts.
Empty attachment lists and new default handoff fields do not change old turn
or submission fingerprints, so interrupted requests can recover after rollout.
New conversations use rubric `quote-intake-v1.14` and prompt
`quote-intake-v1.15`; the prompt explicitly distinguishes automatic consultation
requests from quote requests and from booked appointments.

## Request and photo behavior

The application validates `ready` and `consultationReady`. A consultation
submission stores `handoffType=consultation_request`, waits for a usable ZIP
and completed scan upload, and returns the existing durable quote-request
receipt. The app only confirms success after receiving that receipt. Pause,
unsent text, staged photos and photo loading prevent automatic dispatch.

Photos are provider evidence, never Claude input. The model input excludes
photo bytes, metadata, storage paths, URLs, scan geometry and legacy history.
The backend accepts up to three photos per turn and twelve per request,
validates decoded size/dimensions before conversion, normalizes to JPEG and
creates thumbnails. Objects stay in the existing private `metashape-exports`
bucket under the authenticated homeowner/home/intake namespace. Provider links
use the existing signed gallery. Direct submissions reject foreign, malformed
or traversal paths before creating a receipt or sending email.

The intake route permits a 26,000,000-byte JSON body for three 6 MB photos plus
base64 overhead, controlled by `LIDARAI_QUOTE_INTAKE_MAX_REQUEST_BYTES`. Other
routes keep the existing global limit. New uploads contain `dataBase64`;
recovery returns `sha256` fingerprints instead. A fingerprint-only request is
accepted only when it exactly matches an already saved pending/completed turn.
Retries reuse stored photos. The phone preserves raw bytes in its local pending
draft and keeps unsent selections per homeowner/home. A late picker result
cannot populate another home or overwrite a newer selection. Interrupted and
failed photo selection stays paused across relaunch until the homeowner retries,
explicitly sends the draft, or discards the selection. ImageIO limits
actual output pixels to 2048 independently of screen scale.

## Operations delivery and deployment order

Reuse the existing durable email pipeline for both handoff types, with distinct
consultation subjects and Noah's existing submission copy. Delivery payloads,
IDs and delivered stamps survive restarts. The worker now also checks the
durable outbox after 60 seconds of idle time, so failures can retry without a
restart. Delivered requests are skipped. Resend receives the same idempotency
key across uncertain retries; SMTP's stable Message-ID alone cannot guarantee
provider-side deduplication after an ambiguous delivery.

Before production rollout:

1. Confirm hosted `LIDARAI_OPS_EMAIL`, `LIDARAI_OPS_EMAIL_FROM`,
   `LIDARAI_OPS_QUOTE_CC`, `LIDARAI_RESEND_API_KEY` (or SMTP configuration),
   Supabase credentials and `LIDARAI_PUBLIC_BASE_URL`. Use the intended support
   inbox and a verified sender. Do not copy secrets into documentation.
2. Deploy the reviewed backend first. A backend `main` push automatically
   deploys through the existing Render blueprint. Keep the current single
   process/instance; scaling email delivery needs shared delivery claims.
3. Verify authenticated quote and consultation receipts and a captured/test
   delivery in an isolated environment. Check hosted worker logs for startup,
   failed-send recovery and delivery stamps. Confirm the configured support
   recipient and Noah copy using a controlled test, not a real homeowner lead.
4. Integrate the mobile change. Hold TestFlight until Noah finishes the other
   requested changes and explicitly authorizes a release.

Read-only production checks on October 27: `/health` returned `ok`;
`metashape-exports` is private; `quote_intakes` has RLS and neither `anon` nor
`authenticated` can SELECT it. Storage read policies scope authenticated
homeowners to their own first path component. These checks do not establish
hosted email credentials, worker operation, the deployed PR SHA, or an
authenticated end-to-end submission. Production data/configuration was not
modified and no real leads or messages were sent during integration tests.

## Validation

Baseline reproduced Quintin's full backend result: 1,399 passed, 2 skipped.
Regression coverage adds pinned-schema upgrades, old retry fingerprints,
interrupted photo recovery, model input isolation, storage ownership,
decompression limits, request-size limits, consultation receipt recovery,
photo selection races and worker retries. Tests capture external delivery.

Commands (repository-relative):

```sh
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q
PYTHONPATH=backend .venv/bin/python -m app.quote_intake.rubrics
PYTHONPATH=backend .venv/bin/python backend/scripts/evaluate_quote_intake.py
# From backend/, synthetic text only; no submission/email API is imported:
PYTHONPATH=. ../.venv/bin/python scripts/evaluate_quote_intake.py --live \
  --fixtures tests/fixtures/quote_intake/integration_v1.json \
  --output docs/evaluations/quote-intake-integration-2026-10-27.json
```

iOS checks use Xcode 26.5, iPhone 17 Pro simulator / iOS 26.5, normal simulator
signing and `-parallel-testing-enabled NO`. The full unit suite and focused
dashboard checks are distinct from a complete UI suite or physical-device
testing. The existing unwrap-memory known issue remains tracked.

Observed results:

- Full backend: **1,417 passed, 2 skipped**. The six existing FastAPI lifespan
  deprecation warnings remain. Rubric and evaluation fixture validation passed.
- Full iOS unit suite: **658 tests in 85 suites passed**, with the existing
  unwrap-memory known issue. The final photo-selection safeguards are also
  verified by the focused 11-test intake suite.
- Five focused simulator flows passed across the final runs: automatic quote
  submission with spoken ZIP, pending submission recovery after relaunch,
  pause while upload waits, consultation submission, and returned quote PDFs.
  The consultation UI fixture initially lacked a completed upload; the app
  correctly waited. Seeding the completed upload allowed receipt/success checks
  to pass. These are fixture-based tests, not production submissions.
- Live Claude (`claude-sonnet-5`, effort `low`, prompt v1.15): **5/5 scenarios**,
  **9 homeowner turns, 14 model calls**, median turn latency **15.4 seconds**,
  maximum **23.3 seconds**. One question field repeated because the synthetic
  persona answered only the first half of a two-part question; inspection found
  no question repeated after its answer was supplied. Submitted leads/emails: 0.

The full iOS run is recorded in
`Test-LidarAI-2026.10.27_14-32-46--0400.xcresult`; the earlier four passing UI
regressions are in `Test-LidarAI-2026.10.27_14-27-37--0400.xcresult`. Both live
reports are committed under `docs/evaluations/`. Do not treat an earlier failing
test run or the initial live fixture report as the final verification record.

Live evaluation reports retain exact replies and cited answers. The initial
window-cleaning and landscape-design fixtures incorrectly expected completion
before answering newly required rubric fields; the recorded questions were
appropriate. The final adaptive fixtures answer those questions without
removing rubric requirements. This small sample is not an exhaustive evaluation
of all trades or a guarantee of semantic accuracy. Physical iPhone photo-picker,
scan/upload and production email delivery checks remain release work.
